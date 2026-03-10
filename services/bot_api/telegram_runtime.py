from __future__ import annotations

import base64
import html
import json
import re
import shlex
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from libs.config.settings import BotApiSettings
from libs.db.pool import DatabasePool
from libs.domain.buyer import BuyerService
from libs.domain.deposit_intents import DepositIntentService
from libs.domain.errors import (
    DomainError,
    DuplicateOrderError,
    InsufficientFundsError,
    InvalidStateError,
    ListingValidationError,
    NoSlotsAvailableError,
    NotFoundError,
    PayloadValidationError,
)
from libs.domain.fx_rates import FxRateService
from libs.domain.ledger import FinanceService
from libs.domain.seller import SellerService
from libs.integrations.fx_rates import CoinGeckoUsdtRubClient
from libs.integrations.wb import WbPingClient
from libs.integrations.wb_public import (
    WbObservedBuyerPrice,
    WbProductSnapshot,
    WbPublicApiError,
    WbPublicCatalogClient,
)
from libs.logging.setup import EventLogger, get_logger
from libs.security.token_cipher import decrypt_token, encrypt_token
from services.bot_api.buyer_handlers import BuyerCommandProcessor
from services.bot_api.callback_data import (
    CALLBACK_VERSION,
    CallbackPayload,
    build_callback,
    parse_callback,
)
from services.bot_api.seller_handlers import SellerCommandProcessor

try:
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        MenuButtonCommands,
        Message,
        Update,
    )
    from telegram.ext import (
        Application,
        CallbackContext,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:  # pragma: no cover - checked at runtime on deployment hosts
    raise RuntimeError(
        "python-telegram-bot is required for webhook runtime. "
        "Install dependencies from pyproject/requirements before running bot webhook mode."
    ) from exc


_ROLE_SELLER = "seller"
_ROLE_BUYER = "buyer"
_ROLE_ADMIN = "admin"

_ACTIVE_ROLE_KEY = "active_role"
_LAST_BUYER_SHOP_SLUG_KEY = "last_buyer_shop_slug"
_PROMPT_STATE_KEY = "prompt_state"
_SELLER_LISTINGS_PAGE_KEY = "seller_listings_page"
_USDT_SUMMARY_QUANT = Decimal("0.1")
_USDT_EXACT_QUANT = Decimal("0.000001")
_RUB_QUANT = Decimal("1")
_LISTING_COLLATERAL_FEE_MULTIPLIER = Decimal("1.01")
_BUYER_TASK_COMPANION_PRODUCTS = 2
_NUMBERED_PAGE_SIZE = 10
_MSK_TZ = ZoneInfo("Europe/Moscow")

_SELLER_COMMAND_PREFIXES = (
    "/shop_",
    "/token_set",
    "/listing_",
)
_BUYER_COMMAND_PREFIXES = (
    "/shop",
    "/reserve",
    "/submit_order",
    "/my_orders",
)
_RUNTIME_REQUIRED_SCHEMA_COLUMNS = {
    "users": {
        "is_seller",
        "is_buyer",
        "is_admin",
    },
    "assignments": {
        "wb_product_id",
    },
    "buyer_orders": {
        "wb_product_id",
    },
    "listings": {
        "display_title",
        "wb_source_title",
        "search_phrase",
        "wb_subject_name",
        "wb_brand_name",
        "wb_vendor_code",
        "wb_description",
        "wb_photo_url",
        "wb_tech_sizes_json",
        "wb_characteristics_json",
        "reference_price_rub",
        "reference_price_source",
        "reference_price_updated_at",
    },
}


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None


class _BotHealthServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        payload_factory: Callable[[], dict[str, Any]],
        logger: EventLogger,
    ) -> None:
        self._host = host
        self._port = port
        self._payload_factory = payload_factory
        self._logger = logger
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        owner = self

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path not in {"/healthz", "/health"}:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = owner._payload_factory()
                status_code = 200 if bool(payload.get("ready")) else 503
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        self._server = ThreadingHTTPServer((self._host, self._port), HealthHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._logger.info(
            "bot_health_server_started",
            health_host=self._host,
            health_port=self._port,
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._logger.info("bot_health_server_stopped")


class TelegramWebhookRuntime:
    """Real Telegram webhook runtime with button-first role shell."""

    def __init__(self, *, settings: BotApiSettings, logger: EventLogger | None = None) -> None:
        self._settings = settings
        self._logger = logger or get_logger(__name__)
        self._admin_telegram_ids = set(settings.admin_telegram_ids)
        self._ready = False
        self._startup_error: str | None = None
        self._health_server: _BotHealthServer | None = None
        self._db_pool = DatabasePool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        self._seller_service: SellerService | None = None
        self._buyer_service: BuyerService | None = None
        self._finance_service: FinanceService | None = None
        self._deposit_service: DepositIntentService | None = None
        self._fx_rate_service: FxRateService | None = None
        self._seller_processor: SellerCommandProcessor | None = None
        self._buyer_processor: BuyerCommandProcessor | None = None
        self._wb_ping_client: WbPingClient | None = None
        self._wb_public_client: WbPublicCatalogClient | None = None
        self._display_rub_per_usdt = settings.display_rub_per_usdt

    def run(self) -> None:
        webhook_url = self._build_webhook_url()
        tls_enabled = bool(
            self._settings.webhook_tls_cert_path and self._settings.webhook_tls_key_path,
        )
        self._logger.info(
            "telegram_webhook_runtime_starting",
            webhook_url=webhook_url,
            listen_host=self._settings.webhook_listen_host,
            listen_port=self._settings.webhook_listen_port,
            webhook_path=self._settings.webhook_path,
            webhook_tls_enabled=tls_enabled,
            callback_version=CALLBACK_VERSION,
            admins_count=len(self._admin_telegram_ids),
        )
        application = self._build_application()
        self._health_server = _BotHealthServer(
            host=self._settings.bot_health_host,
            port=self._settings.bot_health_port,
            payload_factory=self._health_payload,
            logger=self._logger,
        )
        self._health_server.start()
        run_kwargs: dict[str, Any] = {}
        if tls_enabled:
            run_kwargs["cert"] = self._settings.webhook_tls_cert_path
            run_kwargs["key"] = self._settings.webhook_tls_key_path
        try:
            application.run_webhook(
                listen=self._settings.webhook_listen_host,
                port=self._settings.webhook_listen_port,
                url_path=self._settings.webhook_path,
                webhook_url=webhook_url,
                secret_token=self._settings.webhook_secret_token,
                drop_pending_updates=False,
                allowed_updates=Update.ALL_TYPES,
                **run_kwargs,
            )
        finally:
            self._health_server.stop()

    def _build_application(self) -> Application:
        if not self._settings.telegram_bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is required for webhook runtime. "
                "Use --seller-command/--buyer-command for local command adapter mode."
            )
        application = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self._handle_start))
        application.add_handler(MessageHandler(filters.COMMAND, self._handle_command_message))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        application.add_handler(CallbackQueryHandler(self._handle_callback))
        application.add_error_handler(self._handle_error)
        return application

    async def _post_init(self, application: Application) -> None:
        self._ready = False
        self._startup_error = None
        try:
            await self._db_pool.open()
            await self._db_pool.check()
            await self._assert_runtime_schema_compatibility()

            self._seller_service = SellerService(self._db_pool.pool)
            self._buyer_service = BuyerService(self._db_pool.pool)
            self._finance_service = FinanceService(self._db_pool.pool)
            self._deposit_service = DepositIntentService(
                self._db_pool.pool,
                invoice_ttl_hours=self._settings.seller_collateral_invoice_ttl_hours,
            )
            self._fx_rate_service = FxRateService(
                self._db_pool.pool,
                provider=CoinGeckoUsdtRubClient(
                    endpoint=self._settings.fx_rate_provider_url,
                    timeout_seconds=self._settings.fx_rate_timeout_seconds,
                ),
                refresh_lock_id=self._settings.fx_rate_refresh_lock_id,
            )
            await self._deposit_service.ensure_default_shard(
                shard_key=self._settings.seller_collateral_shard_key,
                deposit_address=self._settings.seller_collateral_shard_address,
                chain=self._settings.seller_collateral_shard_chain,
                asset=self._settings.seller_collateral_shard_asset,
            )
            wb_ping_client = WbPingClient(
                timeout_seconds=self._settings.wb_ping_timeout_seconds,
                max_requests=self._settings.wb_ping_rate_limit_count,
                window_seconds=self._settings.wb_ping_rate_limit_window_seconds,
            )
            self._wb_ping_client = wb_ping_client
            self._wb_public_client = WbPublicCatalogClient(
                content_timeout_seconds=self._settings.wb_content_timeout_seconds,
                orders_timeout_seconds=self._settings.wb_orders_timeout_seconds,
                orders_lookback_days=self._settings.wb_orders_lookback_days,
            )
            self._seller_processor = SellerCommandProcessor(
                seller_service=self._seller_service,
                wb_ping_client=wb_ping_client,
                token_cipher_key=self._settings.token_cipher_key,
                bot_username=self._settings.telegram_bot_username,
            )
            self._buyer_processor = BuyerCommandProcessor(
                buyer_service=self._buyer_service,
                bot_username=self._settings.telegram_bot_username,
            )

            bot_profile = await application.bot.get_me()
            self._logger.info(
                "telegram_webhook_bot_identity",
                telegram_bot_id=bot_profile.id,
                telegram_bot_username=bot_profile.username,
            )
            try:
                await application.bot.set_my_commands(
                    [BotCommand(command="start", description="Открыть меню")],
                )
                await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                self._logger.info("telegram_menu_button_configured")
            except Exception as exc:
                self._logger.warning(
                    "telegram_menu_button_config_failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                )
            if self._settings.webhook_set_enabled:
                await self._ensure_webhook_registration(application=application)
            self._ready = True
            self._logger.info("telegram_webhook_runtime_ready")
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._logger.exception(
                "telegram_webhook_runtime_init_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            try:
                await self._db_pool.close()
            except Exception:
                self._logger.exception("telegram_webhook_runtime_init_close_failed")
            raise

    async def _post_shutdown(self, application: Application) -> None:
        self._ready = False
        await self._db_pool.close()
        self._logger.info("telegram_webhook_runtime_stopped")

    async def _ensure_webhook_registration(self, *, application: Application) -> None:
        desired_url = self._build_webhook_url()
        cert_path = self._settings.webhook_tls_cert_path
        key_path = self._settings.webhook_tls_key_path
        has_custom_certificate = bool(cert_path and key_path)
        webhook_info = await application.bot.get_webhook_info()
        webhook_matches = (
            webhook_info.url == desired_url
            and webhook_info.has_custom_certificate is has_custom_certificate
        )
        if webhook_matches:
            self._logger.info(
                "telegram_webhook_registration_reused",
                webhook_url=desired_url,
                pending_update_count=webhook_info.pending_update_count,
            )
            return

        if has_custom_certificate:
            assert cert_path is not None  # narrowed by has_custom_certificate
            cert_file = Path(cert_path)
            if not cert_file.exists():
                raise FileNotFoundError(f"WEBHOOK_TLS_CERT_PATH does not exist: {cert_file}")
            with cert_file.open("rb") as certificate_stream:
                await application.bot.set_webhook(
                    url=desired_url,
                    secret_token=self._settings.webhook_secret_token,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                    certificate=certificate_stream,
                )
        else:
            await application.bot.set_webhook(
                url=desired_url,
                secret_token=self._settings.webhook_secret_token,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
        refreshed = await application.bot.get_webhook_info()
        self._logger.info(
            "telegram_webhook_registered",
            webhook_url=refreshed.url,
            pending_update_count=refreshed.pending_update_count,
            has_custom_certificate=refreshed.has_custom_certificate,
        )

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = _identity_from_update(update)
        if identity is None or update.message is None:
            return

        self._logger.info(
            "telegram_start_received",
            telegram_update_id=update.update_id,
            telegram_id=identity.telegram_id,
        )
        self._clear_prompt(context)
        start_args = " ".join(context.args).strip()
        if start_args.startswith("shop_"):
            shop_slug = start_args[len("shop_") :].strip()
            if shop_slug:
                try:
                    buyer = await self._buyer_service.bootstrap_buyer(
                        telegram_id=identity.telegram_id,
                        username=identity.username,
                    )
                except InvalidStateError as exc:
                    await update.message.reply_text(
                        f"Режим покупателя недоступен: {exc}",
                        reply_markup=self._root_menu_markup(identity=identity),
                    )
                    return
                context.user_data[_ACTIVE_ROLE_KEY] = _ROLE_BUYER
                context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = shop_slug
                await self._send_buyer_shop_catalog(
                    update.message,
                    slug=shop_slug,
                    buyer_user_id=buyer.user_id,
                )
                return

        await update.message.reply_text(
            "Выберите роль:",
            reply_markup=self._root_menu_markup(identity=identity),
        )

    async def _handle_command_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        identity = _identity_from_update(update)
        if identity is None or update.message is None:
            return

        raw_text = (update.message.text or "").strip()
        self._logger.info(
            "telegram_command_received",
            telegram_update_id=update.update_id,
            telegram_id=identity.telegram_id,
            command=raw_text.split(" ", 1)[0].lower(),
        )
        response = await self._dispatch_legacy_command(
            telegram_id=identity.telegram_id,
            username=identity.username,
            raw_text=raw_text,
        )
        if response is None:
            await update.message.reply_text(
                "Команда не распознана. Используйте /start и кнопки меню."
            )
            return

        if response.delete_source_message:
            command = raw_text.split(" ", 1)[0].lower()
            suppress_delete_notice = command == "/token_set"
            await self._delete_sensitive_message(
                update.message,
                notify=not suppress_delete_notice,
            )

        await update.message.reply_text(response.text)

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = _identity_from_update(update)
        if identity is None or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return

        prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
        if isinstance(prompt_state, dict):
            await self._handle_prompt_message(
                update=update,
                context=context,
                identity=identity,
                text=text,
                prompt_state=prompt_state,
            )
            return

        active_role = context.user_data.get(_ACTIVE_ROLE_KEY)
        if active_role == _ROLE_SELLER:
            await update.message.reply_text(
                "Используйте кнопки меню продавца.",
                reply_markup=self._seller_menu_markup(),
            )
            return
        if active_role == _ROLE_BUYER:
            await update.message.reply_text(
                "Используйте кнопки меню покупателя.",
                reply_markup=self._buyer_menu_markup(),
            )
            return
        if active_role == _ROLE_ADMIN:
            await update.message.reply_text(
                "Используйте кнопки меню администратора.",
                reply_markup=self._admin_menu_markup(),
            )
            return

        await update.message.reply_text(
            "Используйте /start, чтобы открыть меню.",
            reply_markup=self._root_menu_markup(identity=identity),
        )

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        raw_payload = query.data or ""
        try:
            payload = parse_callback(raw_payload)
        except ValueError:
            await query.answer("Кнопка устарела", show_alert=True)
            return

        try:
            await query.answer()
        except Exception as exc:
            self._logger.warning(
                "telegram_callback_answer_failed",
                telegram_update_id=update.update_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
        if query.message is None:
            try:
                await query.answer(
                    "⚠️ Не удалось обновить экран. Отправьте /start и повторите действие.",
                    show_alert=True,
                )
            except Exception as exc:
                self._logger.warning(
                    "telegram_callback_missing_message_alert_failed",
                    telegram_update_id=update.update_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                )
            return
        await self._retire_message_keyboard(query.message)
        identity = _identity_from_callback(update)
        if identity is None:
            return
        self._logger.info(
            "telegram_callback_received",
            telegram_update_id=update.update_id,
            flow=payload.flow,
            action=payload.action,
            entity_id=payload.entity_id,
            telegram_id=identity.telegram_id,
        )

        if payload.flow == "root":
            await self._handle_root_callback(
                context=context,
                identity=identity,
                payload=payload,
                query_message=query.message,
            )
            return
        if payload.flow == _ROLE_SELLER:
            await self._handle_seller_callback(
                context=context,
                identity=identity,
                payload=payload,
                query_message=query.message,
            )
            return
        if payload.flow == _ROLE_BUYER:
            await self._handle_buyer_callback(
                context=context,
                identity=identity,
                payload=payload,
                query_message=query.message,
                callback_query_id=query.id,
                update_id=update.update_id,
            )
            return
        if payload.flow == _ROLE_ADMIN:
            await self._handle_admin_callback(
                context=context,
                identity=identity,
                payload=payload,
                query_message=query.message,
            )
            return

        await query.message.reply_text("Неизвестная кнопка. Отправьте /start.")

    async def _handle_root_callback(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        identity: TelegramIdentity,
        payload: CallbackPayload,
        query_message: Message | None,
    ) -> None:
        if payload.action != "role":
            if query_message is not None:
                await query_message.reply_text("Неизвестное действие root.")
            return

        role = payload.entity_id
        if role == _ROLE_SELLER:
            context.user_data[_ACTIVE_ROLE_KEY] = _ROLE_SELLER
            self._clear_prompt(context)
            try:
                seller = await self._seller_service.bootstrap_seller(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                )
            except InvalidStateError as exc:
                await self._replace_message(
                    query_message,
                    f"Режим продавца недоступен: {exc}",
                    self._root_menu_markup(identity=identity),
                )
                return
            await self._render_seller_dashboard(
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if role == _ROLE_BUYER:
            context.user_data[_ACTIVE_ROLE_KEY] = _ROLE_BUYER
            self._clear_prompt(context)
            try:
                buyer = await self._buyer_service.bootstrap_buyer(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                )
            except InvalidStateError as exc:
                await self._replace_message(
                    query_message,
                    f"Режим покупателя недоступен: {exc}",
                    self._root_menu_markup(identity=identity),
                )
                return
            await self._render_buyer_dashboard(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if role == _ROLE_ADMIN:
            if identity.telegram_id not in self._admin_telegram_ids:
                if query_message is not None:
                    await query_message.reply_text("Доступ запрещен: вы не администратор.")
                return
            context.user_data[_ACTIVE_ROLE_KEY] = _ROLE_ADMIN
            self._clear_prompt(context)
            await self._ensure_admin_user(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            await self._render_admin_dashboard(query_message=query_message)
            return

        if query_message is not None:
            await query_message.reply_text("Неизвестная роль.")

    async def _handle_seller_callback(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        identity: TelegramIdentity,
        payload: CallbackPayload,
        query_message: Message | None,
    ) -> None:
        seller = await self._seller_service.bootstrap_seller(
            telegram_id=identity.telegram_id,
            username=identity.username,
        )
        action = payload.action
        if action == "menu":
            self._clear_prompt(context)
            await self._render_seller_dashboard(
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "back":
            self._clear_prompt(context)
            await self._replace_message(
                query_message,
                "Выберите роль:",
                self._root_menu_markup(identity=identity),
            )
            return
        if action in {"shop_create_token_prompt", "prompt_shop_title"}:
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_shop_create_token",
                sensitive=True,
                extra={"seller_user_id": seller.user_id, "notify_sensitive_delete": False},
            )
            await self._replace_message(
                query_message,
                self._shop_token_instruction_text(),
                self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                parse_mode="HTML",
            )
            return
        if action == "shops":
            await self._render_seller_shops(
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "shop_open":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть магазин. Нажмите кнопку еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            await self._render_seller_shop_details(
                query_message=query_message,
                seller_user_id=seller.user_id,
                shop_id=int(payload.entity_id),
            )
            return
        if action == "shop_delete_preview":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось выбрать магазин для удаления. Попробуйте еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            await self._render_shop_delete_preview(
                query_message=query_message,
                seller_user_id=seller.user_id,
                shop_id=int(payload.entity_id),
            )
            return
        if action == "shop_delete_confirm":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось выбрать магазин для удаления. Попробуйте еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            await self._execute_shop_delete(
                query_message=query_message,
                seller_user_id=seller.user_id,
                shop_id=int(payload.entity_id),
            )
            return
        if action == "shop_rename_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось выбрать магазин для переименования. Попробуйте еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            shop_id = int(payload.entity_id)
            try:
                shop = await self._seller_service.get_shop(
                    seller_user_id=seller.user_id,
                    shop_id=shop_id,
                )
            except NotFoundError:
                await self._replace_message(
                    query_message,
                    "Магазин не найден.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_shop_rename",
                sensitive=False,
                extra={
                    "shop_id": shop_id,
                    "seller_user_id": seller.user_id,
                    "token_is_valid": self._is_valid_shop_token(shop.wb_token_status),
                },
            )
            await self._replace_message(
                query_message,
                (
                    f"Переименование магазина «{shop.title}».\n\n"
                    "⚠️ Важно: при переименовании ссылка магазина изменится. "
                    "Старая ссылка перестанет работать для покупателей.\n"
                    "Название магазина видят покупатели, поэтому используйте нейтральное и "
                    "понятное имя.\n\n"
                    "Введите новое название магазина следующим сообщением."
                ),
                self._seller_shop_detail_markup(
                    shop_id=shop_id,
                    token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
                ),
            )
            return
        if action == "shop_token_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть настройки токена. Попробуйте еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            shop_id = int(payload.entity_id)
            try:
                shop = await self._seller_service.get_shop(
                    seller_user_id=seller.user_id,
                    shop_id=shop_id,
                )
            except NotFoundError:
                await self._replace_message(
                    query_message,
                    "Магазин не найден.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_shop_token",
                sensitive=True,
                extra={
                    "shop_id": shop_id,
                    "seller_user_id": seller.user_id,
                    "notify_sensitive_delete": False,
                },
            )
            await self._replace_message(
                query_message,
                self._shop_token_instruction_text(shop_title=shop.title),
                self._seller_shop_detail_markup(
                    shop_id=shop_id,
                    token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
                ),
                parse_mode="HTML",
            )
            return
        if action == "listings":
            requested_page = self._coerce_page_number(payload.entity_id)
            await self._render_seller_listings(
                context=context,
                query_message=query_message,
                seller_user_id=seller.user_id,
                page=requested_page,
            )
            return
        if action == "listing_create_pick_shop":
            await self._render_listing_create_shop_picker(
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "listing_create_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось выбрать магазин. Попробуйте еще раз.",
                    self._seller_shops_menu_markup(has_shops=True),
                )
                return
            shop_id = int(payload.entity_id)
            try:
                shop = await self._seller_service.get_shop(
                    seller_user_id=seller.user_id,
                    shop_id=shop_id,
                )
            except NotFoundError:
                await self._render_seller_shops(
                    query_message=query_message,
                    seller_user_id=seller.user_id,
                    notice="Магазин не найден или уже удален.",
                )
                return
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_listing_create",
                sensitive=False,
                extra={
                    "shop_id": shop_id,
                    "shop_title": shop.title,
                    "seller_user_id": seller.user_id,
                },
            )
            await self._refresh_display_rub_per_usdt()
            await self._replace_message(
                query_message,
                self._listing_create_instruction_text(shop_title=shop.title),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к объявлениям",
                                callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action == "listing_open":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть объявление. Попробуйте еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._render_seller_listing_detail(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
                list_page=self._seller_listings_page_from_context(context),
            )
            return
        if action == "listing_activation_blocked":
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Объявление не готово к активации",
                    lines=[
                        "Сейчас на балансе не хватает средств для обеспечения объявления.",
                    ],
                    note=(
                        "Пополните баланс, затем вернитесь к карточке объявления "
                        "и активируйте его."
                    ),
                    warning=True,
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="➕ Пополнить",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="topup_prompt",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ К объявлениям",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listings",
                                    entity_id=str(self._seller_listings_page_from_context(context)),
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action == "listing_title_keep":
            await self._create_listing_from_prompt(
                context=context,
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "listing_title_edit_prompt":
            prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
            if not isinstance(prompt_state, dict):
                await self._replace_message(
                    query_message,
                    "Не удалось продолжить создание объявления. Откройте раздел заново.",
                    self._seller_back_markup(action="listings", label="↩️ К объявлениям"),
                )
                return
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_listing_title_edit",
                sensitive=False,
                extra={
                    key: value
                    for key, value in prompt_state.items()
                    if key not in {"role", "type", "sensitive"}
                },
            )
            await self._replace_message(
                query_message,
                self._listing_title_edit_prompt_text(
                    current_title=str(prompt_state.get("suggested_display_title", "")).strip()
                ),
                self._seller_back_markup(action="listings", label="↩️ К объявлениям"),
                parse_mode="HTML",
            )
            return
        if action == "listing_edit":
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Редактирование отключено",
                    lines=[
                        "Редактирование объявлений недоступно, чтобы не создавать конфликтов "
                        "с уже начатыми заданиями покупателей.",
                    ],
                    note=(
                        "Если нужно изменить параметры, создайте новое объявление "
                        "и удалите старое."
                    ),
                    warning=True,
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ К объявлениям",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listings",
                                    entity_id=str(self._seller_listings_page_from_context(context)),
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action in {
            "listing_edit_title",
            "listing_edit_search",
            "listing_edit_cashback",
            "listing_edit_slots",
            "listing_edit_confirm",
        }:
            self._clear_prompt(context)
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Редактирование отключено",
                    lines=[
                        "Изменение объявления недоступно.",
                    ],
                    note="Создайте новое объявление с нужными параметрами и удалите старое.",
                    warning=True,
                ),
                parse_mode="HTML",
            )
            return
        if action == "listing_activate":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить объявление. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_activate(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
                list_page=self._seller_listings_page_from_context(context),
            )
            return
        if action == "listing_pause":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить объявление. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_pause(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
                list_page=self._seller_listings_page_from_context(context),
            )
            return
        if action == "listing_unpause":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить объявление. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_unpause(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
                list_page=self._seller_listings_page_from_context(context),
            )
            return
        if action == "listing_delete_preview":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить объявление. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._render_listing_delete_preview(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
                list_page=self._seller_listings_page_from_context(context),
            )
            return
        if action == "listing_delete_confirm":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить объявление. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_delete(
                context=context,
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "balance":
            await self._render_seller_balance(
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "topup_prompt":
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_topup_amount",
                sensitive=False,
                extra={"seller_user_id": seller.user_id},
            )
            await self._replace_message(
                query_message,
                (
                    "Введите сумму пополнения в USDT (например, 1.2).\n"
                    "Бот автоматически рассчитает точную сумму для перевода."
                ),
                self._seller_balance_menu_markup(),
            )
            return
        if action == "topup_history":
            await self._render_seller_topup_history(
                query_message=query_message,
                seller_user_id=seller.user_id,
                page=self._coerce_page_number(payload.entity_id),
            )
            return
        if action == "topup_help":
            await self._render_seller_topup_help(query_message=query_message)
            return

        await self._replace_message(
            query_message,
            "Неизвестное действие продавца.",
            self._seller_menu_markup(),
        )

    async def _render_seller_dashboard(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        listings = await self._seller_service.list_listing_collateral_views(
            seller_user_id=seller_user_id
        )
        balance = await self._seller_service.get_seller_balance_snapshot(
            seller_user_id=seller_user_id
        )
        orders = await self._load_seller_order_counters(seller_user_id=seller_user_id)

        listings_active = sum(1 for item in listings if item.status == "active")
        listings_total = len(listings)
        shops_total = len(shops)
        shops_active = sum(1 for item in shops if self._is_valid_shop_token(item.wb_token_status))
        balance_free = balance.seller_available_usdt
        balance_total = balance.seller_available_usdt + balance.seller_collateral_usdt

        text = self._screen_text(
            title="Кабинет продавца",
            cta="Выберите раздел ниже.",
            lines=[
                f"<b>Магазины:</b> {shops_total} · {shops_active} активно",
                f"<b>Объявления:</b> {listings_total} · {listings_active} активно",
                "<b>Задания:</b> "
                f"{orders['in_progress']} в процессе · "
                f"{orders['completed']} оформлено · "
                f"{orders['picked_up']} выкуплено",
                f"<b>Баланс:</b> {self._format_usdt_with_rub(balance_total)}",
                f"<b>Свободно:</b> {self._format_usdt_with_rub(balance_free)}",
            ],
            note="Откройте объявления, магазины или баланс в зависимости от задачи.",
        )
        await self._replace_message(
            query_message,
            text,
            self._seller_menu_markup(),
            parse_mode="HTML",
        )

    async def _load_seller_order_counters(self, *, seller_user_id: int) -> dict[str, int]:
        async with self._db_pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE a.status IN (
                                    'reserved',
                                    'order_submitted',
                                    'order_verified',
                                    'picked_up_wait_unlock'
                                )
                            ),
                            0
                        ) AS in_progress,
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE a.status IN (
                                    'eligible_for_withdrawal',
                                    'withdraw_pending_admin',
                                    'withdraw_sent'
                                )
                            ),
                            0
                        ) AS completed,
                        COALESCE(COUNT(*) FILTER (WHERE a.pickup_at IS NOT NULL), 0) AS picked_up
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    WHERE l.seller_user_id = %s
                      AND l.deleted_at IS NULL
                    """,
                    (seller_user_id,),
                )
                row = await cur.fetchone()
                return {
                    "in_progress": int(row["in_progress"]),
                    "completed": int(row["completed"]),
                    "picked_up": int(row["picked_up"]),
                }

    async def _render_seller_shops(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        notice: str | None = None,
    ) -> None:
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        if not shops:
            lines = ["Магазинов пока нет."]
            if notice:
                lines.insert(0, html.escape(notice))
            text = self._screen_text(
                title="Магазины",
                lines=lines,
                note="Нажмите «➕ Создать магазин», чтобы добавить первый магазин.",
            )
            await self._replace_message(
                query_message,
                text,
                self._seller_shops_menu_markup(has_shops=False),
                parse_mode="HTML",
            )
            return

        lines = ["Выберите магазин."]
        if notice:
            lines.insert(0, html.escape(notice))
        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=f"🏬 {shop.title}",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="shop_open",
                        entity_id=str(shop.shop_id),
                    ),
                )
            ]
            for shop in shops
        ]
        keyboard_rows.extend(self._seller_shops_menu_markup(has_shops=True).inline_keyboard)
        await self._replace_message(
            query_message,
            self._screen_text(title="Магазины", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _render_seller_shop_details(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        shop_id: int,
        notice: str | None = None,
    ) -> None:
        try:
            shop = await self._seller_service.get_shop(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
            )
        except NotFoundError:
            await self._render_seller_shops(
                query_message=query_message,
                seller_user_id=seller_user_id,
                notice="Магазин не найден или уже удален.",
            )
            return

        deep_link = f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"
        lines = [
            f"<b>Название:</b> {html.escape(shop.title)}",
            f"<b>Ссылка для покупателей:</b>\n{html.escape(deep_link)}",
            (
                "<b>Токен WB API:</b> активно"
                if self._is_valid_shop_token(shop.wb_token_status)
                else "<b>Токен WB API:</b> неактивно"
            ),
        ]
        if notice:
            lines.insert(0, html.escape(notice))
        await self._replace_message(
            query_message,
            self._screen_text(
                title=f"Магазин «{html.escape(shop.title)}»",
                lines=lines,
                note="Название магазина видят покупатели.",
            ),
            self._seller_shop_detail_markup(
                shop_id=shop_id,
                token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
            ),
            parse_mode="HTML",
        )

    def _seller_shops_menu_markup(self, *, has_shops: bool) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Создать магазин",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="shop_create_token_prompt",
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                    )
                ],
            ]
        )

    def _seller_back_markup(self, *, action: str, label: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=build_callback(flow=_ROLE_SELLER, action=action),
                    )
                ]
            ]
        )

    def _seller_shop_detail_markup(
        self,
        *,
        shop_id: int,
        token_is_valid: bool = False,
    ) -> InlineKeyboardMarkup:
        token_label = "✅ Токен WB API" if token_is_valid else "❌ Токен WB API"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=token_label,
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="shop_token_prompt",
                            entity_id=str(shop_id),
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Переименовать",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="shop_rename_prompt",
                            entity_id=str(shop_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="shop_delete_preview",
                            entity_id=str(shop_id),
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ К списку магазинов",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                    )
                ],
            ]
        )

    def _shop_token_instruction_text(self, *, shop_title: str | None = None) -> str:
        title = (
            f"Токен WB API для магазина «{html.escape(shop_title)}»"
            if shop_title
            else "Создание магазина"
        )
        lines = [
            "<b>Шаг 1 из 2.</b>",
            (
                "<b>Как создать:</b> Создайте Базовый токен в режиме "
                "«Только для чтения» с категориями: Контент, Статистика, Вопросы и отзывы."
            ),
            (
                "<b>Где найти:</b> ЛК ВБ -> Интеграции по API -> "
                "Создать токен -> Для интеграции вручную."
            ),
            (
                "<b>Зачем нужен токен:</b> для получения информации о товаре, "
                "проверки статуса заказов и отзывов."
            ),
            (
                "<b>Безопасно:</b> токен создается только в режиме чтения, "
                "поэтому изменить данные с ним невозможно."
            ),
        ]
        note = (
            "Сначала бот проверит токен, и только потом попросит название магазина."
            if shop_title is None
            else "Сообщение с токеном будет удалено автоматически."
        )
        return self._screen_text(
            title=title,
            cta="Отправьте токен WB API следующим сообщением ниже.",
            lines=lines,
            note=note,
        )

    def _listing_create_instruction_text(self, *, shop_title: str) -> str:
        fx_text = self._format_decimal(self._display_rub_per_usdt, quant=Decimal("0.01"))
        return self._screen_text(
            title=f"Создание объявления для магазина «{html.escape(shop_title)}»",
            cta="Отправьте сообщение с информацией об объявлении согласно формату ниже.",
            lines=[
                (
                    "<b>Формат:</b> "
                    "<code>&lt;артикул ВБ&gt; &lt;кэшбэк руб&gt; "
                    "&lt;макс заказов&gt; &lt;поисковая фраза&gt;</code>"
                ),
                "<b>Пример:</b> <code>12345678 100 5 \"женские джинсы\"</code>",
                (
                    f"<b>Кэшбэк:</b> сумма для покупателя. "
                    f"Конвертация в $ произойдет по текущему курсу ~{fx_text}."
                ),
                "<b>Макс заказов:</b> количество покупателей по этому объявлению.",
                "<b>Поисковая фраза:</b> запрос, по которому покупатель будет искать товар.",
            ],
            note=(
                "После этого бот подтянет карточку товара, попробует определить цену "
                "покупателя по заказам за 30 дней и попросит подтвердить данные."
            ),
        )

    def _listing_title_review_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Сохранить текущее название",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_title_keep",
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить название",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_title_edit_prompt",
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ К объявлениям",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                    )
                ],
            ]
        )

    def _listing_title_edit_prompt_text(self, *, current_title: str) -> str:
        return self._screen_text(
            title="Название объявления",
            cta="Отправьте новое название следующим сообщением ниже.",
            lines=[
                f"<b>Текущее название:</b> {html.escape(current_title)}",
            ],
            note="Название увидят покупатели.",
        )

    def _listing_title_confirmation_text(
        self,
        *,
        wb_product_id: int,
        search_phrase: str,
        cashback_rub: Decimal,
        slot_count: int,
        snapshot: WbProductSnapshot,
        suggested_display_title: str,
        buyer_price_rub: int,
        reference_price_source: str,
        observed_buyer_price: WbObservedBuyerPrice | None = None,
    ) -> str:
        reward_usdt = (cashback_rub / self._display_rub_per_usdt).quantize(
            _USDT_EXACT_QUANT,
            rounding=ROUND_HALF_UP,
        )
        collateral_required_usdt = (
            reward_usdt * Decimal(slot_count) * _LISTING_COLLATERAL_FEE_MULTIPLIER
        ).quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
        cashback_percent = self._format_listing_cashback_percent(
            reference_price_rub=buyer_price_rub,
            cashback_rub=cashback_rub,
        )
        lines = [
            f"<b>Товар:</b> {html.escape(snapshot.name)}",
            f"<b>Артикул ВБ:</b> {wb_product_id}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(search_phrase)}&quot;",
            f"<b>Цена покупателя:</b> {self._format_price_rub(buyer_price_rub)}",
            f"<b>Кэшбэк:</b> {self._format_usdt_with_rub(reward_usdt)}",
            f"<b>Кэшбэк, %:</b> {cashback_percent}",
            f"<b>Макс. заказов:</b> {slot_count}",
            f"<b>Обеспечение:</b> {self._format_usdt_with_rub(collateral_required_usdt)}",
            f"<b>Название для покупателей:</b> {html.escape(suggested_display_title)}",
        ]
        if observed_buyer_price is not None and reference_price_source == "orders":
            lines.append(
                "<b>Источник цены:</b> заказы за 30 дней, "
                f"цена продавца "
                f"{self._format_price_rub(observed_buyer_price.seller_price_rub)}, "
                f"СПП {observed_buyer_price.spp_percent}%."
            )
        if reference_price_source == "manual":
            lines.append("<b>Источник цены:</b> введена вручную.")
        return self._screen_text(
            title="Проверьте объявление",
            cta="Проверьте данные объявления и выберите следующее действие ниже.",
            lines=lines,
            note="Если название подходит, сохраните его. Если нет, отредактируйте название.",
        )

    def _listing_manual_price_prompt_text(
        self,
        *,
        wb_product_id: int,
        snapshot: WbProductSnapshot,
    ) -> str:
        return self._screen_text(
            title="Нужна цена покупателя",
            cta="Введите текущую цену покупателя в рублях следующим сообщением ниже.",
            lines=[
                "Карточка товара найдена, но по заказам за 30 дней цена не определилась.",
                f"<b>Артикул ВБ:</b> {wb_product_id}",
                f"<b>Предмет:</b> {html.escape(snapshot.subject_name or '—')}",
                f"<b>Бренд:</b> {html.escape(snapshot.brand or '—')}",
                f"<b>Артикул продавца:</b> {html.escape(snapshot.vendor_code or '—')}",
                f"<b>Название WB:</b> {html.escape(snapshot.name)}",
            ],
            note="Укажите цену с учетом всех скидок. Пример: 392.",
        )

    def _format_listing_cashback_percent(
        self,
        *,
        reference_price_rub: int | Decimal | None,
        cashback_rub: Decimal,
    ) -> str:
        if reference_price_rub is None:
            return "—"
        reference = Decimal(str(reference_price_rub))
        if reference <= Decimal("0"):
            return "—"
        percent = (cashback_rub / reference * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
        return f"~{percent}%"

    @staticmethod
    def _coerce_page_number(raw_value: str | None) -> int:
        if not raw_value:
            return 1
        try:
            page = int(raw_value)
        except (TypeError, ValueError):
            return 1
        return page if page > 0 else 1

    @staticmethod
    def _resolve_numbered_page(
        *,
        total_items: int,
        requested_page: int,
        page_size: int = _NUMBERED_PAGE_SIZE,
    ) -> tuple[int, int, int, int]:
        if total_items <= 0:
            return 1, 1, 0, 0
        total_pages = (total_items + page_size - 1) // page_size
        page = max(1, min(requested_page, total_pages))
        start_index = (page - 1) * page_size
        end_index = min(start_index + page_size, total_items)
        return page, total_pages, start_index, end_index

    def _seller_listings_page_from_context(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        return self._coerce_page_number(str(context.user_data.get(_SELLER_LISTINGS_PAGE_KEY, "1")))

    @staticmethod
    def _set_seller_listings_page(
        context: ContextTypes.DEFAULT_TYPE | None,
        *,
        page: int,
    ) -> None:
        if context is None:
            return
        context.user_data[_SELLER_LISTINGS_PAGE_KEY] = page

    def _numbered_page_markup(
        self,
        *,
        flow: str,
        open_action: str,
        page_action: str,
        item_ids: list[int],
        start_number: int,
        page: int,
        total_pages: int,
        extra_rows: list[list[InlineKeyboardButton]] | None = None,
        back_row: list[InlineKeyboardButton] | None = None,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        current_row: list[InlineKeyboardButton] = []
        for offset, item_id in enumerate(item_ids):
            current_row.append(
                InlineKeyboardButton(
                    text=str(start_number + offset),
                    callback_data=build_callback(
                        flow=flow,
                        action=open_action,
                        entity_id=str(item_id),
                    ),
                )
            )
            if len(current_row) == 5:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if page > 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=build_callback(
                            flow=flow,
                            action=page_action,
                            entity_id=str(page - 1),
                        ),
                    )
                )
            if page < total_pages:
                nav_row.append(
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=build_callback(
                            flow=flow,
                            action=page_action,
                            entity_id=str(page + 1),
                        ),
                    )
                )
            if nav_row:
                rows.append(nav_row)

        if extra_rows:
            rows.extend(extra_rows)
        if back_row:
            rows.append(back_row)
        return InlineKeyboardMarkup(rows)

    def _listing_edit_menu_markup(self, *, listing_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="✏️ Название",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit_title",
                            entity_id=str(listing_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🔎 Поиск",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit_search",
                            entity_id=str(listing_id),
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💸 Кэшбэк",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit_cashback",
                            entity_id=str(listing_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🔢 Кол-во",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit_slots",
                            entity_id=str(listing_id),
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ К карточке",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_open",
                            entity_id=str(listing_id),
                        ),
                    )
                ],
            ]
        )

    def _listing_edit_confirm_markup(self, *, listing_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Сохранить изменения",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit_confirm",
                            entity_id=str(listing_id),
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ К редактированию",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_edit",
                            entity_id=str(listing_id),
                        ),
                    )
                ],
            ]
        )

    def _listing_edit_field_prompt_text(
        self,
        *,
        field_label: str,
        current_value: str,
        hint: str,
    ) -> str:
        return self._screen_text(
            title=f"Редактирование: {field_label}",
            lines=[f"<b>Сейчас:</b> {html.escape(current_value)}"],
            note=hint,
        )

    def _listing_edit_confirmation_text(
        self,
        *,
        listing,
        new_display_title: str,
        new_search_phrase: str,
        new_reward_usdt: Decimal,
        new_slot_count: int,
    ) -> str:
        new_cashback_rub = (new_reward_usdt * self._display_rub_per_usdt).quantize(
            _RUB_QUANT,
            rounding=ROUND_HALF_UP,
        )
        new_collateral = (
            new_reward_usdt * Decimal(new_slot_count) * _LISTING_COLLATERAL_FEE_MULTIPLIER
        ).quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
        current_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        cashback_percent = self._format_listing_cashback_percent(
            reference_price_rub=listing.reference_price_rub,
            cashback_rub=new_cashback_rub,
        )
        return self._screen_text(
            title="Подтвердите изменения",
            lines=[
                (
                    f"<b>Название:</b> "
                    f"{html.escape(current_title)} "
                    f"-> {html.escape(new_display_title)}"
                ),
                (
                    f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot; "
                    f"-> &quot;{html.escape(new_search_phrase)}&quot;"
                ),
                (
                    f"<b>Кэшбэк:</b> {self._format_usdt_with_rub(listing.reward_usdt)} "
                    f"-> {self._format_usdt_with_rub(new_reward_usdt)}"
                ),
                (
                    f"<b>Кэшбэк, %:</b> {cashback_percent}"
                ),
                f"<b>Макс. заказов:</b> {listing.slot_count} -> {new_slot_count}",
                (
                    f"<b>Обеспечение:</b> "
                    f"{self._format_usdt_with_rub(listing.collateral_required_usdt)} "
                    f"-> {self._format_usdt_with_rub(new_collateral)}"
                ),
            ],
            note="Сохраните изменения, если все выглядит верно.",
        )

    def _listing_created_prompt_activation_text(
        self,
        *,
        display_title: str,
        wb_product_id: int,
        wb_subject_name: str | None,
        wb_vendor_code: str | None,
        wb_source_title: str | None,
        wb_brand_name: str | None,
        reference_price_rub: int | None,
        reference_price_source: str | None,
        search_phrase: str,
        cashback_rub: Decimal,
        reward_usdt: Decimal,
        slot_count: int,
        collateral_required_usdt: Decimal,
    ) -> str:
        cashback_percent = self._format_listing_cashback_percent(
            reference_price_rub=reference_price_rub,
            cashback_rub=cashback_rub,
        )
        lines = [
            f"<b>Товар:</b> {html.escape(display_title)}",
            f"<b>Артикул ВБ:</b> {wb_product_id}",
            f"<b>Поисковая фраза:</b> &quot;{html.escape(search_phrase)}&quot;",
            f"<b>Цена покупателя:</b> {self._format_price_optional_rub(reference_price_rub)}",
            f"<b>Кэшбэк:</b> {self._format_usdt_with_rub(reward_usdt)}",
            f"<b>Кэшбэк, %:</b> {cashback_percent}",
            f"<b>Макс. заказов:</b> {slot_count}",
            f"<b>Обеспечение:</b> {self._format_usdt_with_rub(collateral_required_usdt)}",
        ]
        if wb_subject_name:
            lines.append(f"<b>Предмет:</b> {html.escape(wb_subject_name)}")
        if wb_vendor_code:
            lines.append(f"<b>Артикул продавца:</b> {html.escape(wb_vendor_code)}")
        if wb_source_title:
            lines.append(f"<b>Название WB:</b> {html.escape(wb_source_title)}")
        if wb_brand_name:
            lines.append(f"<b>Бренд WB:</b> {html.escape(wb_brand_name)}")
        if reference_price_source == "manual":
            lines.append("<b>Источник цены:</b> введена вручную.")
        elif reference_price_source == "orders":
            lines.append("<b>Источник цены:</b> рассчитана по заказам за 30 дней.")
        return self._screen_text(
            title="Проверьте объявление перед активацией",
            lines=lines,
            note=(
                "Если все верно, активируйте объявление. "
                "После активации поделитесь ссылкой на магазин."
            ),
        ) + "\n\n<b>Активировать объявление сейчас?</b>"

    async def _render_shop_delete_preview(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        shop_id: int,
    ) -> None:
        try:
            shop = await self._seller_service.get_shop(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
            )
            preview = await self._seller_service.get_shop_delete_preview(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
            )
        except NotFoundError:
            await self._render_seller_shops(
                query_message=query_message,
                seller_user_id=seller_user_id,
                notice="Магазин не найден или уже удален.",
            )
            return

        text = self._screen_text(
            title=f"Удаление магазина «{html.escape(shop.title)}» необратимо",
            lines=[
                f"Активных объявлений: {preview.active_listings_count}",
                f"Активных заданий: {preview.open_assignments_count}",
                (
                    "Покупателям будет выплачен кэшбэк: "
                    f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}"
                ),
                (
                    "Продавцу вернется: "
                    f"{self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}"
                ),
            ],
            note=(
                "При удалении магазина активные задания будут считаться выполненными, "
                "а кэшбэк будет выплачен покупателям."
            ),
            warning=True,
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="✅ Подтвердить удаление",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="shop_delete_confirm",
                                entity_id=str(shop_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Отмена",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="shop_open",
                                entity_id=str(shop_id),
                            ),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _execute_shop_delete(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        shop_id: int,
    ) -> None:
        try:
            result = await self._seller_service.delete_shop(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
                deleted_by_user_id=seller_user_id,
                idempotency_key=f"tg-shop-delete:{seller_user_id}:{shop_id}",
            )
        except NotFoundError:
            await self._render_seller_shops(
                query_message=query_message,
                seller_user_id=seller_user_id,
                notice="Магазин не найден или уже удален.",
            )
            return

        if not result.changed:
            message = "Магазин уже удален."
        else:
            message = (
                "Магазин удален.\n"
                "Покупателям ушло: "
                f"{self._format_usdt_with_rub(result.assignment_transferred_usdt)}\n"
                "Продавцу вернулось: "
                f"{self._format_usdt_with_rub(result.unassigned_collateral_returned_usdt)}"
            )
        self._logger.info(
            "seller_shop_deleted",
            shop_id=shop_id,
            assignment_transferred_usdt=str(result.assignment_transferred_usdt),
            unassigned_collateral_returned_usdt=str(result.unassigned_collateral_returned_usdt),
        )
        await self._render_seller_shops(
            query_message=query_message,
            seller_user_id=seller_user_id,
            notice=message,
        )

    async def _render_seller_listings(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE | None,
        query_message: Message | None,
        seller_user_id: int,
        page: int = 1,
        notice: str | None = None,
    ) -> None:
        listings = await self._seller_service.list_listing_collateral_views(
            seller_user_id=seller_user_id
        )
        if not listings:
            lines = ["Объявлений пока нет."]
            if notice:
                lines.insert(0, html.escape(notice))
            text = self._screen_text(
                title="Объявления",
                lines=lines,
                note="Нажмите «➕ Создать объявление», чтобы добавить первое объявление.",
            )
            await self._replace_message(
                query_message,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="➕ Создать объявление",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listing_create_pick_shop",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад",
                                callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        shop_slugs = {shop.shop_id: shop.slug for shop in shops}
        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(listings),
            requested_page=page,
        )
        self._set_seller_listings_page(context, page=resolved_page)
        page_items = listings[start_index:end_index]
        lines = []
        if notice:
            lines.append(html.escape(notice))
        for number, listing in enumerate(page_items, start=start_index + 1):
            display_title = self._listing_display_title(
                display_title=listing.display_title,
                fallback=listing.search_phrase,
            )
            cashback_text = self._format_cashback_rub_with_percent(
                reward_usdt=listing.reward_usdt,
                reference_price_rub=listing.reference_price_rub,
            )
            shop_slug = shop_slugs.get(listing.shop_id)
            shop_link = (
                f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop_slug}"
                if shop_slug
                else "—"
            )
            lines.append(
                f"<b>{number}. {html.escape(display_title)}</b>\n"
                f"<b>Артикул WB:</b> {listing.wb_product_id}\n"
                f"<b>Кэшбэк:</b> {cashback_text}\n"
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;\n"
                + (
                    f"<b>План по заказам / В процессе:</b> "
                    f"{listing.slot_count} / {listing.in_progress_assignments_count}"
                )
                + "\n"
                + f"<b>Ссылка на магазин:</b> {html.escape(shop_link)}\n"
                + (
                    "<b>Обеспечение:</b> "
                    f"{self._format_listing_collateral_line(collateral_view=listing)}"
                )
                + "\n"
                + (
                    f"<b>Статус:</b> "
                    f"{self._listing_activity_badge(is_active=listing.status == 'active')}"
                )
            )
        title = "Объявления"
        if total_pages > 1:
            title = f"Объявления · стр. {resolved_page}/{total_pages}"
        await self._replace_message(
            query_message,
            self._screen_text(
                title=title,
                cta="Нажмите номер ниже, чтобы открыть карточку объявления.",
                lines=lines,
                note="Если нужно новое объявление, используйте кнопку создания ниже.",
                separate_blocks=True,
            ),
            self._numbered_page_markup(
                flow=_ROLE_SELLER,
                open_action="listing_open",
                page_action="listings",
                item_ids=[item.listing_id for item in page_items],
                start_number=start_index + 1,
                page=resolved_page,
                total_pages=total_pages,
                extra_rows=[
                    [
                        InlineKeyboardButton(
                            text="➕ Создать объявление",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listing_create_pick_shop",
                            ),
                        )
                    ]
                ],
                back_row=[
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                    )
                ],
            ),
            parse_mode="HTML",
        )

    async def _render_seller_listing_detail(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
        notice: str | None = None,
    ) -> None:
        try:
            listing = await self._seller_service.get_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
            shop = await self._seller_service.get_shop(
                seller_user_id=seller_user_id,
                shop_id=listing.shop_id,
            )
        except NotFoundError:
            await self._render_seller_listings(
                context=None,
                query_message=query_message,
                seller_user_id=seller_user_id,
                page=list_page,
                notice="Объявление не найдено или уже удалено.",
            )
            return
        views = await self._seller_service.list_listing_collateral_views(
            seller_user_id=seller_user_id,
        )
        collateral_view = next((item for item in views if item.listing_id == listing_id), None)
        await self._reply_with_photo_if_available(
            query_message,
            photo_url=listing.wb_photo_url,
        )
        await self._replace_message(
            query_message,
            self._seller_listing_detail_html(
                listing=listing,
                collateral_view=collateral_view,
                shop_link=(
                    f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"
                ),
                notice=notice,
            ),
            self._seller_listing_detail_markup(
                listing_id=listing.listing_id,
                status=listing.status,
                list_page=list_page,
                can_activate=self._listing_has_sufficient_collateral(collateral_view),
            ),
            parse_mode="HTML",
        )

    async def _render_pending_listing_title_review(
        self,
        *,
        query_message: Message | None,
        prompt_state: dict[str, Any],
    ) -> None:
        wb_product_id = int(prompt_state.get("wb_product_id", 0))
        if wb_product_id < 1:
            await self._replace_message(
                query_message,
                "Не удалось продолжить создание объявления. Откройте раздел заново.",
            )
            return
        await self._reply_with_photo_if_available(
            query_message,
            photo_url=str(prompt_state.get("wb_photo_url", "")).strip() or None,
        )
        await self._replace_message(
            query_message,
            self._listing_title_confirmation_text(
                wb_product_id=wb_product_id,
                search_phrase=str(prompt_state.get("search_phrase", "")).strip(),
                cashback_rub=Decimal(str(prompt_state.get("cashback_rub", "0"))),
                slot_count=int(prompt_state.get("slot_count", 0)),
                snapshot=WbProductSnapshot(
                    wb_product_id=wb_product_id,
                    subject_name=str(prompt_state.get("wb_subject_name", "")).strip() or None,
                    vendor_code=str(prompt_state.get("wb_vendor_code", "")).strip() or None,
                    brand=str(prompt_state.get("wb_brand_name", "")).strip() or None,
                    name=str(prompt_state.get("wb_source_title", "")).strip(),
                    description=str(prompt_state.get("wb_description", "")).strip() or None,
                    photo_url=str(prompt_state.get("wb_photo_url", "")).strip() or None,
                    tech_sizes=list(prompt_state.get("wb_tech_sizes") or []),
                    characteristics=list(prompt_state.get("wb_characteristics") or []),
                ),
                suggested_display_title=str(
                    prompt_state.get("suggested_display_title", "")
                ).strip(),
                buyer_price_rub=int(prompt_state.get("reference_price_rub", 0)),
                reference_price_source=str(prompt_state.get("reference_price_source", "")),
                observed_buyer_price=(
                    WbObservedBuyerPrice(
                        buyer_price_rub=int(prompt_state.get("reference_price_rub", 0)),
                        seller_price_rub=int(prompt_state.get("seller_price_rub", 0)),
                        spp_percent=int(prompt_state.get("spp_percent", 0)),
                        observed_at=(
                            datetime.fromisoformat(str(prompt_state.get("reference_price_updated_at")))
                            if prompt_state.get("reference_price_updated_at")
                            else None
                        ),
                    )
                    if prompt_state.get("reference_price_source") == "orders"
                    else None
                ),
            ),
            self._listing_title_review_markup(),
            parse_mode="HTML",
        )

    async def _create_listing_from_prompt(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
        if not isinstance(prompt_state, dict):
            await self._replace_message(
                query_message,
                "Не удалось продолжить создание объявления. Откройте раздел заново.",
            )
            return
        try:
            listing = await self._seller_service.create_listing_draft(
                seller_user_id=seller_user_id,
                shop_id=int(prompt_state.get("shop_id", 0)),
                wb_product_id=int(prompt_state.get("wb_product_id", 0)),
                display_title=str(prompt_state.get("suggested_display_title", "")).strip(),
                wb_source_title=str(prompt_state.get("wb_source_title", "")).strip(),
                wb_subject_name=str(prompt_state.get("wb_subject_name", "")).strip() or None,
                wb_brand_name=str(prompt_state.get("wb_brand_name", "")).strip() or None,
                wb_vendor_code=str(prompt_state.get("wb_vendor_code", "")).strip() or None,
                wb_description=str(prompt_state.get("wb_description", "")).strip() or None,
                wb_photo_url=str(prompt_state.get("wb_photo_url", "")).strip() or None,
                wb_tech_sizes=list(prompt_state.get("wb_tech_sizes") or []),
                wb_characteristics=list(prompt_state.get("wb_characteristics") or []),
                reference_price_rub=(
                    int(prompt_state["reference_price_rub"])
                    if prompt_state.get("reference_price_rub") is not None
                    else None
                ),
                reference_price_source=(
                    str(prompt_state.get("reference_price_source", "")).strip() or None
                ),
                reference_price_updated_at=(
                    datetime.fromisoformat(str(prompt_state.get("reference_price_updated_at")))
                    if prompt_state.get("reference_price_updated_at")
                    else None
                ),
                search_phrase=str(prompt_state.get("search_phrase", "")).strip(),
                reward_usdt=Decimal(str(prompt_state.get("reward_usdt", "0"))),
                slot_count=int(prompt_state.get("slot_count", 0)),
            )
        except (ValueError, NotFoundError, InvalidStateError, InsufficientFundsError):
            await self._replace_message(
                query_message,
                "Не удалось создать объявление. Проверьте данные и попробуйте снова.",
                self._seller_back_markup(action="listings", label="↩️ К объявлениям"),
            )
            return
        self._clear_prompt(context)
        await self._replace_message(
            query_message,
            self._listing_created_prompt_activation_text(
                display_title=listing.display_title or listing.search_phrase,
                wb_product_id=listing.wb_product_id,
                wb_subject_name=listing.wb_subject_name,
                wb_vendor_code=listing.wb_vendor_code,
                wb_source_title=listing.wb_source_title,
                wb_brand_name=listing.wb_brand_name,
                reference_price_rub=listing.reference_price_rub,
                reference_price_source=listing.reference_price_source,
                search_phrase=listing.search_phrase,
                cashback_rub=(
                    listing.reward_usdt * self._display_rub_per_usdt
                ).quantize(_RUB_QUANT, rounding=ROUND_HALF_UP),
                reward_usdt=listing.reward_usdt,
                slot_count=listing.slot_count,
                collateral_required_usdt=listing.collateral_required_usdt,
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="✅ Активировать",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listing_activate",
                                entity_id=str(listing.listing_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📦 К объявлениям",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _render_seller_listing_edit_menu(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
    ) -> None:
        try:
            listing = await self._seller_service.get_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except NotFoundError:
            await self._replace_message(query_message, "Объявление не найдено.")
            return
        display_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Редактирование объявления",
                lines=[
                    f"<b>Товар:</b> {html.escape(display_title)}",
                    "Выберите, что хотите изменить.",
                ],
                note="Артикул WB и данные карточки товара в этом шаге не меняются.",
            ),
            self._listing_edit_menu_markup(listing_id=listing_id),
            parse_mode="HTML",
        )

    async def _render_listing_create_shop_picker(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        if not shops:
            await self._replace_message(
                query_message,
                "Нет доступных магазинов. Сначала создайте магазин.",
                self._seller_shops_menu_markup(has_shops=False),
            )
            return

        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text=f"🏬 {shop.title}",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listing_create_prompt",
                        entity_id=str(shop.shop_id),
                    ),
                )
            ]
            for shop in shops
        ]
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к объявлениям",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Новое объявление",
                lines=["Выберите магазин, для которого хотите создать объявление."],
            ),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _execute_listing_activate(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
    ) -> None:
        try:
            listing = await self._seller_service.get_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
            await self._validate_listing_product_availability(
                seller_user_id=seller_user_id,
                shop_id=listing.shop_id,
                wb_product_id=listing.wb_product_id,
            )
            result = await self._seller_service.activate_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                idempotency_key=f"tg-listing-activate:{seller_user_id}:{listing_id}",
            )
        except NotFoundError:
            await self._replace_message(query_message, "Объявление не найдено.")
            return
        except ListingValidationError as exc:
            await self._replace_message(query_message, str(exc))
            return
        except InvalidStateError:
            await self._replace_message(
                query_message,
                "Не удалось активировать объявление. Проверьте токен магазина и обеспечение.",
            )
            return
        except InsufficientFundsError:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Недостаточно средств для активации",
                    lines=[
                        "На балансе не хватает средств, чтобы зарезервировать обеспечение.",
                    ],
                    note="Пополните баланс и попробуйте снова.",
                    warning=True,
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="➕ Пополнить",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="topup_prompt",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ К карточке",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listing_open",
                                    entity_id=str(listing_id),
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        if result.changed:
            message = "Объявление активно."
        else:
            message = "Объявление уже активно."
        self._logger.info("seller_listing_activated", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listing_detail(
            query_message=query_message,
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice=message,
        )

    async def _execute_listing_pause(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
    ) -> None:
        try:
            result = await self._seller_service.pause_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                reason="manual_pause",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(query_message, "Не удалось поставить объявление на паузу.")
            return

        if result.changed:
            message = "Объявление поставлено на паузу."
        else:
            message = "Объявление уже на паузе."
        self._logger.info("seller_listing_paused", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listing_detail(
            query_message=query_message,
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice=message,
        )

    async def _execute_listing_unpause(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
    ) -> None:
        try:
            listing = await self._seller_service.get_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
            await self._validate_listing_product_availability(
                seller_user_id=seller_user_id,
                shop_id=listing.shop_id,
                wb_product_id=listing.wb_product_id,
            )
            result = await self._seller_service.unpause_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except NotFoundError:
            await self._replace_message(query_message, "Объявление не найдено.")
            return
        except ListingValidationError as exc:
            await self._replace_message(query_message, str(exc))
            return
        except InvalidStateError:
            await self._replace_message(query_message, "Не удалось снять паузу с объявления.")
            return

        if result.changed:
            message = "Объявление снова активно."
        else:
            message = "Объявление уже активно."
        self._logger.info("seller_listing_unpaused", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listing_detail(
            query_message=query_message,
            seller_user_id=seller_user_id,
            listing_id=listing_id,
            list_page=list_page,
            notice=message,
        )

    async def _render_listing_delete_preview(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
        list_page: int = 1,
    ) -> None:
        try:
            preview = await self._seller_service.get_listing_delete_preview(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except NotFoundError:
            await self._replace_message(query_message, "Объявление не найдено.")
            return

        text = self._screen_text(
            title="Удаление объявления необратимо",
            lines=[
                f"Активных заданий по объявлению: {preview.open_assignments_count}",
                (
                    "Покупателям будет выплачен кэшбэк: "
                    f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}"
                ),
                (
                    "Продавцу вернется: "
                    f"{self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}"
                ),
            ],
            note=(
                "При удалении объявления все активные задания будут считаться выполненными, "
                "кэшбэк будет выплачен покупателям."
            ),
            warning=True,
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="✅ Подтвердить удаление",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listing_delete_confirm",
                                entity_id=str(listing_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Отмена",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listing_open",
                                entity_id=str(listing_id),
                            ),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _execute_listing_delete(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE | None,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
    ) -> None:
        try:
            result = await self._seller_service.delete_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                deleted_by_user_id=seller_user_id,
                idempotency_key=f"tg-listing-delete:{seller_user_id}:{listing_id}",
            )
        except NotFoundError:
            await self._replace_message(query_message, "Объявление не найдено.")
            return

        if not result.changed:
            message = "Объявление уже удалено."
        else:
            message = (
                "Объявление удалено.\n"
                "Покупателям ушло: "
                f"{self._format_usdt_with_rub(result.assignment_transferred_usdt)}\n"
                "Продавцу вернулось: "
                f"{self._format_usdt_with_rub(result.unassigned_collateral_returned_usdt)}"
            )
        self._logger.info(
            "seller_listing_deleted",
            listing_id=listing_id,
            assignment_transferred_usdt=str(result.assignment_transferred_usdt),
            unassigned_collateral_returned_usdt=str(result.unassigned_collateral_returned_usdt),
        )
        await self._render_seller_listings(
            context=context,
            query_message=query_message,
            seller_user_id=seller_user_id,
            page=self._seller_listings_page_from_context(context) if context is not None else 1,
            notice=message,
        )

    async def _render_seller_balance(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        snapshot = await self._seller_service.get_seller_balance_snapshot(
            seller_user_id=seller_user_id
        )
        listings = await self._seller_service.list_listing_collateral_views(
            seller_user_id=seller_user_id
        )
        allocated_total = snapshot.seller_collateral_usdt
        required_total = sum((item.collateral_required_usdt for item in listings), Decimal("0"))
        total_balance = snapshot.seller_available_usdt + snapshot.seller_collateral_usdt
        shortfall = required_total - total_balance
        lines = [
            f"<b>Всего:</b> {self._format_usdt_with_rub(total_balance)}",
            (
                "<b>Свободно для новых объявлений:</b> "
                f"{self._format_usdt_with_rub(snapshot.seller_available_usdt)}"
            ),
            f"<b>Уже выделено под объявления:</b> {self._format_usdt_with_rub(allocated_total)}",
        ]
        if shortfall > Decimal("0.000000"):
            lines.append(
                f"<b>Не хватает для активации:</b> {self._format_usdt_with_rub(shortfall)}"
            )
        text = self._screen_text(
            title="Баланс продавца",
            cta="Выберите следующее действие ниже.",
            lines=lines,
            note="Используйте пополнение, если средств не хватает для активации объявлений.",
        )
        await self._replace_message(
            query_message,
            text,
            self._seller_balance_menu_markup(),
            parse_mode="HTML",
        )

    async def _render_seller_topup_history(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        page: int = 1,
    ) -> None:
        intents = await self._deposit_service.list_seller_deposit_intents(
            seller_user_id=seller_user_id,
            limit=100,
        )
        if not intents:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Транзакции продавца",
                    cta="Здесь отображаются пополнения баланса продавца.",
                    lines=["Транзакций пока нет."],
                    note="Нажмите «➕ Пополнить», чтобы создать счет.",
                ),
                self._seller_balance_menu_markup(),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(intents),
            requested_page=page,
            page_size=8,
        )
        lines: list[str] = []
        for item in intents[start_index:end_index]:
            expected_amount = self._format_usdt_value(item.expected_amount_usdt, precise=True)
            block = (
                f"<b>Пополнение</b>\n"
                f"<b>Сумма:</b> {expected_amount} USDT\n"
                f"<b>Статус:</b> {self._deposit_status_badge(item.status)}\n"
                f"<b>Создан:</b> {self._format_datetime_msk(item.created_at)}\n"
                f"<b>Срок счета:</b> до {self._format_datetime_msk(item.expires_at)}"
            )
            if item.status == "credited" and item.credited_amount_usdt is not None:
                block += (
                    f"\n<b>Зачислено:</b> "
                    f"{self._format_usdt_value(item.credited_amount_usdt, precise=True)} USDT"
                )
            if item.status == "manual_review":
                block += "\n<i>Перевод найден, но нужна проверка администратором.</i>"
            if item.status == "expired":
                block += "\n<i>Если вы оплатили после срока, обратитесь к администратору.</i>"
            lines.append(block)

        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if resolved_page > 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="topup_history",
                            entity_id=str(resolved_page - 1),
                        ),
                    )
                )
            if resolved_page < total_pages:
                nav_row.append(
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="topup_history",
                            entity_id=str(resolved_page + 1),
                        ),
                    )
                )
            if nav_row:
                keyboard_rows.append(nav_row)
        keyboard_rows.extend(self._seller_balance_menu_markup().inline_keyboard)

        await self._replace_message(
            query_message,
            self._screen_text(
                title=(
                    f"Транзакции продавца · стр. {resolved_page}/{total_pages}"
                    if total_pages > 1
                    else "Транзакции продавца"
                ),
                cta="Проверьте статус транзакций ниже.",
                lines=lines,
                note=(
                    "Если перевод завис или счет истек, создайте новый счет "
                    "или обратитесь к администратору."
                ),
                separate_blocks=True,
            ),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _render_seller_topup_help(self, *, query_message: Message | None) -> None:
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Как перевести USDT",
                cta=(
                    "Следуйте шагам ниже, затем вернитесь к сообщению со счетом "
                    "и отправьте точную сумму в сети TON."
                ),
                lines=[
                    (
                        'Зайдите в <a href="https://help.ru.wallet.tg/article/60-znakomstvo-s-wallet">'
                        "официальный кошелек Wallet</a> в Telegram: "
                        '<a href="https://t.me/wallet">@wallet</a>.\n'
                        "Также можно использовать любой другой TON-совместимый кошелек "
                        "или перевести USDT напрямую с криптобиржи."
                    ),
                    (
                        'Пополните Крипто Кошелек, купив необходимый объем USDT, '
                        'например на <a href="https://help.ru.wallet.tg/article/80-kak-kupit-kriptovalutu-na-p2p-markete">'
                        "P2P Маркете</a>.\n"
                        "Самый простой и быстрый способ: "
                        "Крипто Кошелек > Пополнить > P2P Экспресс."
                    ),
                    (
                        "Выведите USDT на предоставленный в боте адрес:\n"
                        "Крипто Кошелек > Вывести > Внешний кошелек или биржа > "
                        "Доллары > Сеть TON."
                    ),
                ],
                note=(
                    "Важно точно указать сумму перевода, так как по ней "
                    "идентифицируется ваш платеж. Адрес и сумма остаются "
                    "в предыдущем сообщении со счетом."
                ),
                separate_blocks=True,
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ К балансу",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="balance"),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🧾 Транзакции",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="topup_history",
                            ),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _handle_buyer_callback(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        identity: TelegramIdentity,
        payload: CallbackPayload,
        query_message: Message | None,
        callback_query_id: str,
        update_id: int,
    ) -> None:
        buyer = await self._buyer_service.bootstrap_buyer(
            telegram_id=identity.telegram_id,
            username=identity.username,
        )
        action = payload.action
        if action == "menu":
            self._clear_prompt(context)
            await self._render_buyer_dashboard(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "back":
            self._clear_prompt(context)
            await self._replace_message(
                query_message,
                "Выберите роль:",
                self._root_menu_markup(identity=identity),
            )
            return
        if action == "shops":
            await self._render_buyer_shops_section(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "open_last_shop":
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            if not slug:
                saved_shops = await self._buyer_service.list_saved_shops(
                    buyer_user_id=buyer.user_id,
                    limit=1,
                )
                if saved_shops:
                    slug = saved_shops[0].slug
            if not slug:
                await self._replace_message(
                    query_message,
                    "Нет сохраненного магазина. Нажмите «🔎 Открыть магазин по коду».",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = slug
            await self._send_buyer_shop_catalog(
                query_message,
                slug=slug,
                buyer_user_id=buyer.user_id,
                prefer_edit=True,
            )
            return
        if action == "open_saved_shop":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть магазин. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            try:
                saved_shop = await self._buyer_service.resolve_saved_shop_for_buyer(
                    buyer_user_id=buyer.user_id,
                    shop_id=int(payload.entity_id),
                )
            except (NotFoundError, ValueError):
                await self._replace_message(
                    query_message,
                    "Этот магазин больше недоступен. Откройте магазин по коду.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = saved_shop.slug
            await self._send_buyer_shop_catalog(
                query_message,
                slug=saved_shop.slug,
                buyer_user_id=buyer.user_id,
                prefer_edit=True,
            )
            return
        if action == "prompt_shop_slug":
            self._set_prompt(
                context,
                role=_ROLE_BUYER,
                prompt_type="buyer_shop_slug",
                sensitive=False,
            )
            await self._replace_message(
                query_message,
                (
                    "Введите код магазина из ссылки.\n"
                    "Это часть после shop_ в ссылке."
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к магазинам",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="shops",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "listing_open":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть товар. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            if not slug:
                await self._replace_message(
                    query_message,
                    "Не удалось определить текущий магазин. Откройте каталог заново.",
                    self._buyer_menu_markup(),
                )
                return
            await self._render_buyer_listing_detail(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                shop_slug=slug,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "reserve":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть выбранный товар. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            await self._execute_buyer_reserve(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                listing_id=int(payload.entity_id),
                callback_query_id=callback_query_id,
            )
            return
        if action == "assignments":
            await self._render_buyer_assignments(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "submit_payload_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть задание. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к заданиям",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            assignment_id = int(payload.entity_id)
            self._set_prompt(
                context,
                role=_ROLE_BUYER,
                prompt_type="buyer_submit_payload",
                sensitive=True,
                extra={"assignment_id": assignment_id},
            )
            await self._replace_message(
                query_message,
                "Вставьте токен-подтверждение из расширения одним сообщением.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к заданиям",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "assignment_cancel_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть задание. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к заданиям",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            assignment_id = int(payload.entity_id)
            assignments = await self._buyer_service.list_buyer_assignments(
                buyer_user_id=buyer.user_id
            )
            assignment = next(
                (item for item in assignments if item.assignment_id == assignment_id),
                None,
            )
            if assignment is None:
                await self._replace_message(
                    query_message,
                    "Задание не найдено.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к заданиям",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            if assignment.status not in {"reserved", "order_submitted"}:
                await self._replace_message(
                    query_message,
                    "Это задание уже нельзя отменить.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к заданиям",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            await self._replace_message(
                query_message,
                "Отказаться от задания?\n"
                "Бронь будет снята, а задание снова станет доступно для других покупателей.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="✅ Отказаться от задания",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignment_cancel_confirm",
                                    entity_id=str(assignment_id),
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к заданиям",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ],
                    ]
                ),
            )
            return
        if action == "assignment_cancel_confirm":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось отменить задание. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к заданиям",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            await self._execute_buyer_assignment_cancel(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                assignment_id=int(payload.entity_id),
                callback_query_id=callback_query_id,
            )
            return
        if action == "balance":
            await self._render_buyer_balance(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "withdraw_full":
            await self._start_withdraw_full_amount(
                context=context,
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "withdraw_prompt_amount":
            self._set_prompt(
                context,
                role=_ROLE_BUYER,
                prompt_type="buyer_withdraw_amount",
                sensitive=False,
                extra={"buyer_user_id": buyer.user_id},
            )
            await self._replace_message(
                query_message,
                "Введите сумму вывода в USDT (например, 4.5).",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к балансу",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="balance",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "withdraw_history":
            await self._render_buyer_withdraw_history(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                page=self._coerce_page_number(payload.entity_id),
            )
            return

        await self._replace_message(
            query_message,
            "Неизвестное действие покупателя.",
            self._buyer_menu_markup(),
        )

    async def _render_buyer_dashboard(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        assignments = await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        snapshot = await self._finance_service.get_buyer_balance_snapshot(
            buyer_user_id=buyer_user_id
        )

        in_progress_statuses = {
            "reserved",
            "order_submitted",
            "order_verified",
            "picked_up_wait_unlock",
        }
        ready_statuses = {"eligible_for_withdrawal", "withdraw_pending_admin"}

        in_progress = sum(1 for item in assignments if item.status in in_progress_statuses)
        ready = sum(1 for item in assignments if item.status in ready_statuses)
        paid = sum(1 for item in assignments if item.status == "withdraw_sent")
        total_balance = snapshot.buyer_available_usdt + snapshot.buyer_withdraw_pending_usdt

        text = self._screen_text(
            title="Кабинет покупателя",
            cta="Выберите раздел ниже.",
            lines=[
                (
                    "<b>Задания:</b> "
                    f"{in_progress} в процессе · {ready} к выводу · "
                    f"{paid} выплачено · {len(assignments)} всего"
                ),
                f"<b>Баланс:</b> {self._format_usdt_with_rub(total_balance)}",
                f"<b>Доступно:</b> {self._format_usdt_with_rub(snapshot.buyer_available_usdt)}",
            ],
            note="Откройте магазины, задания или баланс и вывод в зависимости от следующего шага.",
        )
        await self._replace_message(
            query_message,
            text,
            self._buyer_menu_markup(),
            parse_mode="HTML",
        )

    async def _render_buyer_shops_section(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        lines = ["Откройте магазин по коду или выберите один из сохраненных."]
        saved_shops = await self._buyer_service.list_saved_shops(
            buyer_user_id=buyer_user_id,
            limit=12,
        )
        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="🔎 Открыть магазин по коду",
                    callback_data=build_callback(
                        flow=_ROLE_BUYER,
                        action="prompt_shop_slug",
                    ),
                )
            ]
        ]
        if saved_shops:
            lines.append("<b>Сохраненные магазины:</b>")
            for shop in saved_shops:
                lines.append(f"• {html.escape(shop.title)}")
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"🏪 {shop.title}",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="open_saved_shop",
                                entity_id=str(shop.shop_id),
                            ),
                        )
                    ]
                )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="🔁 Открыть последний магазин",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="open_last_shop",
                        ),
                    )
                ]
            )
        else:
            lines.append("Сохраненных магазинов пока нет.")

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(title="Магазины", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _execute_buyer_reserve(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        listing_id: int,
        callback_query_id: str,
    ) -> None:
        try:
            reservation = await self._buyer_service.reserve_listing_slot(
                buyer_user_id=buyer_user_id,
                listing_id=listing_id,
                idempotency_key=f"tg-reserve:{buyer_user_id}:{listing_id}:{callback_query_id}",
            )
        except NotFoundError:
            await self._replace_message(
                query_message,
                "Товар больше недоступен.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к магазинам",
                                callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                            )
                        ]
                    ]
                ),
            )
            return
        except NoSlotsAvailableError:
            active_same_listing: bool = False
            assignments = await self._buyer_service.list_buyer_assignments(
                buyer_user_id=buyer_user_id
            )
            for item in assignments:
                if item.listing_id == listing_id and item.status not in {
                    "expired_2h",
                    "wb_invalid",
                    "returned_within_14d",
                    "delivery_expired",
                }:
                    active_same_listing = True
                    break

            if active_same_listing:
                await self._replace_message(
                    query_message,
                    "У вас уже есть активное задание по этому товару.\n"
                    "Продолжить можно в разделе «📋 Задания».",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="📋 Мои задания",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ],
                        ]
                    ),
                )
                return

            await self._replace_message(
                query_message,
                "Свободных заданий по этому товару нет. Попробуйте выбрать другой товар.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к магазинам",
                                callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                            )
                        ]
                    ]
                ),
            )
            return
        except InvalidStateError as exc:
            details = str(exc).strip().lower()
            if "already purchased" in details:
                await self._replace_message(
                    query_message,
                    "Этот товар уже был куплен с вашего аккаунта. Повторно забронировать нельзя.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            if "already has assignment" in details:
                await self._replace_message(
                    query_message,
                    "У вас уже есть активное задание по этому товару.\n"
                    "Продолжить можно в разделе «📋 Задания».",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="📋 Мои задания",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="assignments",
                                    ),
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ],
                        ]
                    ),
                )
                return
            await self._replace_message(
                query_message,
                "Не удалось начать задание. Попробуйте снова.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к магазинам",
                                callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                            )
                        ]
                    ]
                ),
            )
            return

        assignments = await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        assignment = next(
            (item for item in assignments if item.assignment_id == reservation.assignment_id),
            None,
        )
        if assignment is None:
            text = self._screen_text(
                title="Задание создано",
                lines=["Откройте раздел «📋 Задания», чтобы продолжить."],
            )
        elif reservation.created:
            text = self._screen_text(
                title="Задание создано",
                lines=[
                    self._buyer_task_instruction_text(assignment),
                    (
                        "<b>Срок отправки:</b> "
                        f"до {self._format_datetime_msk(assignment.reservation_expires_at)}"
                    ),
                ],
                note="После отправки токена-подтверждения бот продолжит проверку автоматически.",
            )
        else:
            text = self._screen_text(
                title="Задание уже активно",
                lines=[
                    self._buyer_task_instruction_text(assignment),
                    (
                        "<b>Срок отправки:</b> "
                        f"до {self._format_datetime_msk(assignment.reservation_expires_at)}"
                    ),
                ],
            )
        self._logger.info(
            "buyer_slot_reserved",
            listing_id=listing_id,
            assignment_id=reservation.assignment_id,
            reservation_created=reservation.created,
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="Ввести токен-подтверждение",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="submit_payload_prompt",
                                entity_id=str(reservation.assignment_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🚫 Отказаться от задания",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="assignment_cancel_prompt",
                                entity_id=str(reservation.assignment_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📋 Мои задания",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="assignments",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к магазинам",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="shops",
                            ),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _render_buyer_listing_detail(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        shop_slug: str,
        listing_id: int,
        notice: str | None = None,
    ) -> None:
        try:
            listings = await self._buyer_service.list_active_listings_by_shop_slug(
                slug=shop_slug,
                buyer_user_id=buyer_user_id,
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Магазин недоступен. Откройте каталог заново.",
                self._buyer_menu_markup(),
            )
            return
        listing = next((item for item in listings if item.listing_id == listing_id), None)
        if listing is None:
            await self._replace_message(
                query_message,
                "Товар больше недоступен.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к магазинам",
                                callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                            )
                        ]
                    ]
                ),
            )
            return
        await self._reply_with_photo_if_available(query_message, photo_url=listing.wb_photo_url)
        await self._replace_message(
            query_message,
            self._buyer_listing_detail_html(listing=listing, notice=notice),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="✅ Выполнить задание",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="reserve",
                                entity_id=str(listing.listing_id),
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к каталогу",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="open_last_shop"),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _execute_buyer_assignment_cancel(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        assignment_id: int,
        callback_query_id: str,
    ) -> None:
        try:
            result = await self._buyer_service.cancel_assignment_by_buyer(
                buyer_user_id=buyer_user_id,
                assignment_id=assignment_id,
                idempotency_key=(
                    f"tg-assignment-cancel:{buyer_user_id}:{assignment_id}:{callback_query_id}"
                ),
            )
        except NotFoundError:
            await self._replace_message(
                query_message,
                "Задание не найдено.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к заданиям",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        except InvalidStateError:
            await self._replace_message(
                query_message,
                "Это задание уже нельзя отменить.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к заданиям",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return

        text = (
            "Задание отменено. Оно снова доступно для других покупателей."
            if result.changed
            else "Задание уже было отменено ранее."
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="📋 Мои задания",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="assignments"),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ К магазинам",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                        )
                    ],
                ]
            ),
        )

    async def _render_buyer_assignments(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        assignments = await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        if not assignments:
            await self._replace_message(
                query_message,
                "📋 У вас пока нет заданий.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="menu",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return

        lines = ["📋 Мои задания:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for idx, item in enumerate(assignments, start=1):
            display_title = self._listing_display_title(
                display_title=item.display_title,
                fallback=item.search_phrase,
            )
            cashback_text = self._format_cashback_with_percent(
                reward_usdt=item.reward_usdt,
                reference_price_rub=item.reference_price_rub,
            )
            lines.append(
                f"<b>Задание {idx}</b> · магазин: {html.escape(item.shop_slug)}\n"
                f"Товар: {html.escape(display_title)}\n"
                f"Статус: {html.escape(self._humanize_assignment_status(item.status))}\n"
                f"Кэшбэк: {cashback_text}"
            )
            if item.order_id:
                lines.append(f"<b>Номер заказа:</b> {html.escape(item.order_id)}")
            if item.status in {"reserved", "order_submitted"}:
                lines.append(self._buyer_task_instruction_text(item))
                lines.append(
                    "<b>Срок отправки:</b> "
                    f"до {self._format_datetime_msk(item.reservation_expires_at)}"
                )
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text="Ввести токен-подтверждение",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="submit_payload_prompt",
                                entity_id=str(item.assignment_id),
                            ),
                        )
                    ]
                )
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text="🚫 Отказаться от задания",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="assignment_cancel_prompt",
                                entity_id=str(item.assignment_id),
                            ),
                        )
                    ]
                )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(title="Задания", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _render_buyer_balance(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        snapshot = await self._finance_service.get_buyer_balance_snapshot(
            buyer_user_id=buyer_user_id
        )
        text = self._screen_text(
            title="Баланс покупателя",
            cta="Выберите следующее действие ниже.",
            lines=[
                f"<b>Доступно:</b> {self._format_usdt_with_rub(snapshot.buyer_available_usdt)}",
                (
                    "<b>В ожидании вывода:</b> "
                    f"{self._format_usdt_with_rub(snapshot.buyer_withdraw_pending_usdt)}"
                ),
            ],
            note=(
                "Вывод оформляется в USDT, поэтому на следующих шагах "
                "сумма будет указана именно в USDT."
            ),
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="💸 Вывести все доступное",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="withdraw_full",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="✍️ Указать сумму вручную",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="withdraw_prompt_amount",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🧾 Транзакции",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="withdraw_history",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _start_withdraw_full_amount(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        snapshot = await self._finance_service.get_buyer_balance_snapshot(
            buyer_user_id=buyer_user_id
        )
        amount = snapshot.buyer_available_usdt
        if amount <= Decimal("0.000000"):
            await self._replace_message(
                query_message,
                "Нет доступного баланса для вывода.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к балансу",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="balance",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return

        self._set_prompt(
            context,
            role=_ROLE_BUYER,
            prompt_type="buyer_withdraw_address",
            sensitive=True,
            extra={
                "buyer_user_id": buyer_user_id,
                "amount_usdt": str(amount),
            },
        )
        await self._replace_message(
            query_message,
            (
                "Введите адрес кошелька для вывода "
                f"{self._format_usdt_value(amount, precise=True)} USDT."
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к балансу",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="balance"),
                        )
                    ]
                ]
            ),
        )

    async def _render_buyer_withdraw_history(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        page: int = 1,
    ) -> None:
        history = await self._finance_service.list_buyer_withdrawal_history(
            buyer_user_id=buyer_user_id
        )
        if not history:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Транзакции покупателя",
                    cta="Здесь отображаются выводы покупателя.",
                    lines=["Транзакций пока нет."],
                    note="Когда появятся заявки на вывод, они будут видны здесь.",
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к балансу",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="balance",
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(history),
            requested_page=page,
            page_size=8,
        )
        lines: list[str] = []
        for item in history[start_index:end_index]:
            block = (
                f"<b>Вывод</b>\n"
                f"<b>Сумма:</b> {self._format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
                f"<b>Статус:</b> {self._withdraw_status_badge(item.status)}\n"
                f"<b>Адрес:</b> {html.escape(item.payout_address)}"
            )
            if item.tx_hash:
                block += f"\n<b>Хэш перевода:</b> {html.escape(item.tx_hash)}"
            lines.append(block)
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if resolved_page > 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="withdraw_history",
                            entity_id=str(resolved_page - 1),
                        ),
                    )
                )
            if resolved_page < total_pages:
                nav_row.append(
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="withdraw_history",
                            entity_id=str(resolved_page + 1),
                        ),
                    )
                )
            if nav_row:
                keyboard_rows.append(nav_row)
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к балансу",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="balance"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(
                title=(
                    f"Транзакции покупателя · стр. {resolved_page}/{total_pages}"
                    if total_pages > 1
                    else "Транзакции покупателя"
                ),
                cta="Проверьте статус выводов ниже.",
                lines=lines,
                note=(
                    "Если вывод отклонен или задержан, проверьте статус "
                    "и при необходимости оформите новую заявку."
                ),
                separate_blocks=True,
            ),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _ensure_admin_user(self, *, telegram_id: int, username: str | None) -> int:
        async with self._db_pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT id, role, is_admin
                        FROM users
                        WHERE telegram_id = %s
                        FOR UPDATE
                        """,
                        (telegram_id,),
                    )
                    existing = await cur.fetchone()
                    if existing is None:
                        await cur.execute(
                            """
                            INSERT INTO users (
                                telegram_id,
                                username,
                                role,
                                is_seller,
                                is_buyer,
                                is_admin
                            )
                            VALUES (%s, %s, 'admin', false, false, true)
                            RETURNING id
                            """,
                            (telegram_id, username),
                        )
                        created = await cur.fetchone()
                        return created["id"]
                    await cur.execute(
                        """
                        UPDATE users
                        SET username = COALESCE(%s, username),
                            is_admin = true,
                            updated_at = timezone('utc', now())
                        WHERE id = %s
                        """,
                        (username, existing["id"]),
                    )
                    return existing["id"]

    async def _render_admin_dashboard(self, *, query_message: Message | None) -> None:
        pending_withdrawals = await self._finance_service.list_pending_withdrawals(limit=1000)
        review_txs = await self._deposit_service.list_admin_review_txs(limit=1000)
        expired_intents = await self._deposit_service.list_admin_expired_intents(limit=1000)

        text = self._screen_text(
            title="Кабинет администратора",
            cta="Выберите раздел ниже.",
            lines=[
                f"<b>Выводы в очереди:</b> {len(pending_withdrawals)}",
                f"<b>Платежи на ручной разбор:</b> {len(review_txs)}",
                f"<b>Просроченные счета:</b> {len(expired_intents)}",
            ],
            note="Откройте выводы, пополнения или исключения в зависимости от текущей задачи.",
        )
        await self._replace_message(
            query_message,
            text,
            self._admin_menu_markup(),
            parse_mode="HTML",
        )

    async def _render_admin_withdrawals_section(self, *, query_message: Message | None) -> None:
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Выводы",
                cta="Выберите действие ниже.",
                lines=["Раздел для обработки заявок на вывод."],
                note="Откройте очередь или перейдите к конкретной заявке по номеру.",
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="📋 Очередь заявок",
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="withdrawals",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔎 Открыть заявку по номеру",
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="prompt_request_id",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _render_admin_deposits_section(self, *, query_message: Message | None) -> None:
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Пополнения",
                cta="Выберите действие ниже.",
                lines=["Раздел для ручных пополнений и исключений по счетам."],
                note="Откройте ручное пополнение или проверьте спорные/просроченные операции.",
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="🏦 Ручное пополнение",
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="manual_deposit_prompt",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚠️ Нужна проверка",
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="deposit_exceptions",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                        )
                    ],
                ]
            ),
            parse_mode="HTML",
        )

    async def _render_admin_pending_withdrawals(self, *, query_message: Message | None) -> None:
        pending = await self._finance_service.list_pending_withdrawals()
        if not pending:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Выводы",
                    lines=["Очередь вывода пуста."],
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="🔎 Открыть заявку по номеру",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="prompt_request_id",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_section",
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        lines: list[str] = []
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for item in pending:
            lines.append(
                f"<b>Заявка #{item.withdrawal_request_id}</b>\n"
                f"Покупатель: {item.buyer_telegram_id} "
                f"(@{html.escape(item.buyer_username or '-')})\n"
                f"Сумма: {self._format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
                f"Кошелек: {html.escape(item.payout_address)}"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🔎 Открыть заявку #{item.withdrawal_request_id}",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_detail",
                            entity_id=str(item.withdrawal_request_id),
                        ),
                    )
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="🏦 Ручное пополнение",
                    callback_data=build_callback(
                        flow=_ROLE_ADMIN,
                        action="manual_deposit_prompt",
                    ),
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=build_callback(flow=_ROLE_ADMIN, action="withdrawals_section"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(title="Очередь вывода", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _render_admin_withdrawal_detail(
        self,
        *,
        query_message: Message | None,
        request_id: int,
    ) -> None:
        try:
            detail = await self._finance_service.get_withdrawal_request_detail(
                request_id=request_id
            )
        except NotFoundError:
            await self._replace_message(
                query_message,
                "Заявка не найдена. Проверьте номер и попробуйте снова.",
            )
            return

        lines = [
            f"<b>Покупатель:</b> {detail.buyer_telegram_id} "
            f"(@{html.escape(detail.buyer_username or '-')})",
            f"<b>Сумма:</b> {self._format_usdt_value(detail.amount_usdt, precise=True)} USDT",
            f"<b>Статус:</b> {html.escape(self._humanize_withdraw_status(detail.status))}",
            f"<b>Кошелек:</b> {html.escape(detail.payout_address)}",
            f"<b>Создана:</b> {self._format_datetime_msk(detail.requested_at)}",
            (
                f"<b>Обработана:</b> {self._format_datetime_msk(detail.processed_at)}"
                if detail.processed_at
                else "<b>Обработана:</b> -"
            ),
            (
                f"<b>Отправлена:</b> {self._format_datetime_msk(detail.sent_at)}"
                if detail.sent_at
                else "<b>Отправлена:</b> -"
            ),
        ]
        if detail.tx_hash:
            lines.append(f"<b>Хэш перевода:</b> {html.escape(detail.tx_hash)}")
        if detail.note:
            lines.append(f"<b>Комментарий:</b> {html.escape(detail.note)}")
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if detail.status == "withdraw_pending_admin":
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Одобрить",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_approve",
                            entity_id=str(detail.withdrawal_request_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_reject_prompt",
                            entity_id=str(detail.withdrawal_request_id),
                        ),
                    ),
                ]
            )
        elif detail.status == "approved":
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="📤 Отметить как отправлено",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_sent_prompt",
                            entity_id=str(detail.withdrawal_request_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_reject_prompt",
                            entity_id=str(detail.withdrawal_request_id),
                        ),
                    ),
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ К очереди",
                    callback_data=build_callback(flow=_ROLE_ADMIN, action="withdrawals"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(title=f"Заявка #{detail.withdrawal_request_id}", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _execute_admin_withdraw_approve(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        admin_user_id: int,
        request_id: int,
    ) -> None:
        try:
            result = await self._finance_service.approve_withdrawal_request(
                request_id=request_id,
                admin_user_id=admin_user_id,
                idempotency_key=f"tg-admin-approve:{admin_user_id}:{request_id}",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось одобрить заявку. Обновите список и попробуйте снова.",
            )
            return

        detail = await self._finance_service.get_withdrawal_request_detail(request_id=request_id)
        if result.changed:
            await self._notify_buyer_withdraw_status(
                context=context,
                buyer_telegram_id=detail.buyer_telegram_id,
                message=(
                    f"Ваша заявка #{request_id} одобрена.\n"
                    f"Сумма: {self._format_usdt_value(detail.amount_usdt, precise=True)} USDT."
                ),
            )
        self._logger.info(
            "admin_withdraw_approved",
            withdrawal_request_id=request_id,
            changed=result.changed,
        )
        await self._render_admin_withdrawal_detail(
            query_message=query_message,
            request_id=request_id,
        )

    async def _execute_admin_withdraw_reject(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        admin_user_id: int,
        request_id: int,
        reason: str,
    ) -> None:
        try:
            detail = await self._finance_service.get_withdrawal_request_detail(
                request_id=request_id
            )
            result = await self._finance_service.reject_withdrawal_request(
                request_id=request_id,
                admin_user_id=admin_user_id,
                pending_account_id=detail.to_account_id,
                buyer_available_account_id=detail.from_account_id,
                reason=reason,
                idempotency_key=f"tg-admin-reject:{admin_user_id}:{request_id}",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось отклонить заявку. Обновите список и попробуйте снова.",
            )
            return

        refreshed = await self._finance_service.get_withdrawal_request_detail(request_id=request_id)
        if result.changed:
            await self._notify_buyer_withdraw_status(
                context=context,
                buyer_telegram_id=refreshed.buyer_telegram_id,
                message=(
                    f"Ваша заявка #{request_id} отклонена.\n"
                    f"Причина: {reason}"
                ),
            )
        self._logger.info(
            "admin_withdraw_rejected",
            withdrawal_request_id=request_id,
            changed=result.changed,
        )
        await self._render_admin_withdrawal_detail(
            query_message=query_message,
            request_id=request_id,
        )

    async def _execute_admin_withdraw_sent(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        admin_user_id: int,
        request_id: int,
        tx_hash: str,
    ) -> None:
        try:
            detail = await self._finance_service.get_withdrawal_request_detail(
                request_id=request_id
            )
            system_payout_account_id = await self._ensure_system_payout_account_id()
            result = await self._finance_service.mark_withdrawal_sent(
                request_id=request_id,
                admin_user_id=admin_user_id,
                pending_account_id=detail.to_account_id,
                system_payout_account_id=system_payout_account_id,
                tx_hash=tx_hash,
                idempotency_key=f"tg-admin-sent:{admin_user_id}:{request_id}",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось отметить заявку как отправленную. Обновите список и попробуйте снова.",
            )
            return

        refreshed = await self._finance_service.get_withdrawal_request_detail(request_id=request_id)
        if result.changed:
            await self._notify_buyer_withdraw_status(
                context=context,
                buyer_telegram_id=refreshed.buyer_telegram_id,
                message=(
                    f"Ваша заявка #{request_id} отправлена.\n"
                    f"Хэш перевода: {tx_hash}"
                ),
            )
        self._logger.info(
            "admin_withdraw_sent",
            withdrawal_request_id=request_id,
            changed=result.changed,
        )
        await self._render_admin_withdrawal_detail(
            query_message=query_message,
            request_id=request_id,
        )

    async def _execute_admin_manual_deposit(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        admin_user_id: int,
        target_telegram_id: int,
        account_kind: str,
        amount_usdt: Decimal,
        external_reference: str,
    ) -> None:
        normalized_account_kind = self._normalize_manual_deposit_account_kind(account_kind)
        try:
            target_user_id, target_account_id = await self._resolve_manual_deposit_target(
                target_telegram_id=target_telegram_id,
                account_kind=normalized_account_kind,
            )
            tx_hash = (
                external_reference[3:].strip()
                if external_reference.lower().startswith("tx:")
                else None
            )
            result = await self._finance_service.manual_deposit_credit(
                admin_user_id=admin_user_id,
                target_user_id=target_user_id,
                target_account_id=target_account_id,
                amount_usdt=amount_usdt,
                external_reference=external_reference,
                idempotency_key=(
                    f"tg-manual-deposit:{admin_user_id}:{target_telegram_id}:"
                    f"{normalized_account_kind}:{amount_usdt}:{external_reference}"
                ),
                tx_hash=tx_hash,
            )
        except (NotFoundError, InvalidStateError, ValueError) as exc:
            details = str(exc).strip().lower()
            if "account_kind" in details:
                error_text = "Неверный тип роли. Используйте `seller` или `buyer`."
            elif "not found" in details:
                error_text = "Пользователь не найден для выбранной роли."
            else:
                error_text = "Не удалось выполнить пополнение. Проверьте данные и попробуйте снова."
            await self._replace_message(
                query_message,
                error_text,
                self._admin_menu_markup(),
            )
            return
        except InsufficientFundsError:
            await self._replace_message(
                query_message,
                "Недостаточно средств на системном счете для этого пополнения.",
                self._admin_menu_markup(),
            )
            return

        if result.created:
            message = "Пополнение зачислено."
        else:
            message = "Такое пополнение уже было учтено ранее."
        await self._replace_message(query_message, message, self._admin_menu_markup())
        if result.created:
            await self._notify_buyer_withdraw_status(
                context=context,
                buyer_telegram_id=target_telegram_id,
                message=(
                    "Баланс пополнен.\n"
                    f"Сумма: {self._format_usdt_value(amount_usdt, precise=True)} USDT."
                ),
            )
        self._logger.info(
            "admin_manual_deposit_processed",
            ledger_entry_id=result.ledger_entry_id,
            target_account_id=target_account_id,
            amount_usdt=str(amount_usdt),
            deposit_created=result.created,
        )

    async def _render_admin_deposit_exceptions(
        self,
        *,
        query_message: Message | None,
    ) -> None:
        review_txs = await self._deposit_service.list_admin_review_txs(limit=20)
        expired_intents = await self._deposit_service.list_admin_expired_intents(limit=20)

        lines = ["⚠️ Пополнения, требующие проверки:"]
        if review_txs:
            lines.append("Платежи на ручной разбор:")
            for tx in review_txs:
                suffix = f"{tx.suffix_code:03d}" if tx.suffix_code is not None else "нет"
                account_hint = (
                    f"Счет: #{tx.matched_intent_id}"
                    if tx.matched_intent_id
                    else "Счет: не найден"
                )
                lines.append(
                    f"Транзакция #{tx.chain_tx_id}\n"
                    f"Сумма: {self._format_usdt_value(tx.amount_usdt, precise=True)} USDT\n"
                    f"Суффикс: {suffix}\n"
                    f"Хэш: {tx.tx_hash}\n"
                    f"Причина: {tx.review_reason or '-'}\n"
                    f"{account_hint}"
                )
        else:
            lines.append("Платежей на ручной разбор нет.")

        if expired_intents:
            lines.append("Просроченные счета:")
            for intent in expired_intents:
                lines.append(
                    f"Счет #{intent.deposit_intent_id}\n"
                    f"Продавец: {intent.seller_telegram_id}\n"
                    "Ожидалось: "
                    f"{self._format_usdt_value(intent.expected_amount_usdt, precise=True)} USDT\n"
                    f"Суффикс: {intent.suffix_code:03d}\n"
                    f"Истек: {self._format_datetime_msk(intent.expires_at)}"
                )
        else:
            lines.append("Просроченных счетов нет.")

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="🔗 Привязать платеж к счету",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposit_attach_prompt",
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🛑 Отменить счет",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposit_cancel_prompt",
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=build_callback(flow=_ROLE_ADMIN, action="deposits_section"),
                    )
                ],
            ]
        )
        await self._replace_message(query_message, "\n\n".join(lines), keyboard)

    async def _execute_admin_deposit_attach(
        self,
        *,
        query_message: Message | None,
        admin_user_id: int,
        chain_tx_id: int,
        deposit_intent_id: int,
    ) -> None:
        try:
            result = await self._deposit_service.credit_intent_from_chain_tx(
                deposit_intent_id=deposit_intent_id,
                chain_tx_id=chain_tx_id,
                idempotency_key=(
                    f"tg-admin-deposit-attach:{admin_user_id}:{chain_tx_id}:{deposit_intent_id}"
                ),
                admin_user_id=admin_user_id,
                allow_expired=True,
            )
        except (NotFoundError, InvalidStateError, ValueError):
            await self._replace_message(
                query_message,
                "Не удалось привязать платеж к счету. Проверьте номера и попробуйте снова.",
                self._admin_menu_markup(),
            )
            return
        except InsufficientFundsError:
            await self._replace_message(
                query_message,
                "Недостаточно средств на системном счете для зачисления.",
                self._admin_menu_markup(),
            )
            return

        if result.changed:
            message = (
                "Платеж привязан к счету и зачислен.\n"
                f"Счет: #{deposit_intent_id}\n"
                f"Транзакция: #{chain_tx_id}"
            )
        else:
            message = (
                "Эта операция уже была выполнена ранее.\n"
                f"Счет: #{deposit_intent_id}\n"
                f"Транзакция: #{chain_tx_id}"
            )
        await self._replace_message(query_message, message, self._admin_menu_markup())
        self._logger.info(
            "admin_deposit_attach_processed",
            chain_tx_id=chain_tx_id,
            deposit_intent_id=deposit_intent_id,
            changed=result.changed,
            ledger_entry_id=result.ledger_entry_id,
        )

    async def _execute_admin_deposit_cancel(
        self,
        *,
        query_message: Message | None,
        admin_user_id: int,
        deposit_intent_id: int,
        reason: str,
    ) -> None:
        try:
            changed = await self._deposit_service.cancel_deposit_intent(
                deposit_intent_id=deposit_intent_id,
                admin_user_id=admin_user_id,
                reason=reason,
                idempotency_key=f"tg-admin-deposit-cancel:{admin_user_id}:{deposit_intent_id}",
            )
        except (NotFoundError, InvalidStateError, ValueError):
            await self._replace_message(
                query_message,
                "Не удалось отменить счет. Проверьте номер и попробуйте снова.",
                self._admin_menu_markup(),
            )
            return

        message = (
            f"Счет #{deposit_intent_id} отменен."
            if changed
            else f"Счет #{deposit_intent_id} уже был отменен ранее."
        )
        await self._replace_message(query_message, message, self._admin_menu_markup())
        self._logger.info(
            "admin_deposit_cancel_processed",
            deposit_intent_id=deposit_intent_id,
            changed=changed,
        )

    async def _resolve_manual_deposit_target(
        self,
        *,
        target_telegram_id: int,
        account_kind: str,
    ) -> tuple[int, int]:
        required_role_by_account_kind = {
            "seller_available": "seller",
            "buyer_available": "buyer",
        }
        required_role = required_role_by_account_kind.get(account_kind)
        if required_role is None:
            raise ValueError("account_kind must be seller|buyer")

        async with self._db_pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT id, role, is_seller, is_buyer, is_admin
                        FROM users
                        WHERE telegram_id = %s
                        FOR UPDATE
                        """,
                        (target_telegram_id,),
                    )
                    user_row = await cur.fetchone()
                    if user_row is None:
                        raise NotFoundError(
                            f"user with telegram_id {target_telegram_id} not found"
                        )
                    has_required_role = False
                    if required_role == "seller":
                        has_required_role = bool(
                            user_row["is_seller"]
                            or user_row["is_admin"]
                            or user_row["role"] in {"seller", "admin"}
                        )
                    elif required_role == "buyer":
                        has_required_role = bool(
                            user_row["is_buyer"]
                            or user_row["is_admin"]
                            or user_row["role"] in {"buyer", "admin"}
                        )
                    if not has_required_role:
                        raise InvalidStateError(
                            f"user capabilities are incompatible with {account_kind}"
                        )

                    account_code = f"user:{user_row['id']}:{account_kind}"
                    await cur.execute(
                        """
                        INSERT INTO accounts (
                            owner_user_id,
                            account_code,
                            account_kind
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (account_code)
                        DO UPDATE SET updated_at = timezone('utc', now())
                        RETURNING id
                        """,
                        (user_row["id"], account_code, account_kind),
                    )
                    account_row = await cur.fetchone()
                    return user_row["id"], account_row["id"]

    async def _ensure_system_payout_account_id(self) -> int:
        async with self._db_pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO accounts (
                            owner_user_id,
                            account_code,
                            account_kind
                        )
                        VALUES (NULL, 'system:system_payout', 'system_payout')
                        ON CONFLICT (account_code)
                        DO UPDATE SET updated_at = timezone('utc', now())
                        RETURNING id
                        """
                    )
                    row = await cur.fetchone()
                    return row["id"]

    @staticmethod
    def _normalize_manual_deposit_account_kind(account_kind: str) -> str:
        normalized = account_kind.strip().lower()
        aliases = {
            "seller": "seller_available",
            "buyer": "buyer_available",
            "seller_available": "seller_available",
            "buyer_available": "buyer_available",
        }
        mapped = aliases.get(normalized)
        if mapped is None:
            raise ValueError("account_kind must be seller|buyer")
        return mapped

    async def _notify_buyer_withdraw_status(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        buyer_telegram_id: int,
        message: str,
    ) -> None:
        try:
            await context.bot.send_message(chat_id=buyer_telegram_id, text=message)
        except Exception as exc:
            self._logger.warning(
                "telegram_buyer_notify_failed",
                buyer_telegram_id=buyer_telegram_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )

    async def _handle_admin_callback(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        identity: TelegramIdentity,
        payload: CallbackPayload,
        query_message: Message | None,
    ) -> None:
        if identity.telegram_id not in self._admin_telegram_ids:
            if query_message is not None:
                await query_message.reply_text("Доступ запрещен: вы не администратор.")
            return

        admin_user_id = await self._ensure_admin_user(
            telegram_id=identity.telegram_id,
            username=identity.username,
        )

        action = payload.action
        if action == "menu":
            self._clear_prompt(context)
            await self._render_admin_dashboard(query_message=query_message)
            return
        if action == "back":
            self._clear_prompt(context)
            await self._replace_message(
                query_message,
                "Выберите роль:",
                self._root_menu_markup(identity=identity),
            )
            return
        if action == "withdrawals_section":
            await self._render_admin_withdrawals_section(query_message=query_message)
            return
        if action == "deposits_section":
            await self._render_admin_deposits_section(query_message=query_message)
            return
        if action == "exceptions_section":
            await self._render_admin_deposit_exceptions(query_message=query_message)
            return
        if action == "withdrawals":
            await self._render_admin_pending_withdrawals(query_message=query_message)
            return
        if action == "withdrawal_detail":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить заявку. Откройте список и попробуйте снова.",
                    self._admin_menu_markup(),
                )
                return
            await self._render_admin_withdrawal_detail(
                query_message=query_message,
                request_id=int(payload.entity_id),
            )
            return
        if action == "withdrawal_approve":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить заявку. Откройте список и попробуйте снова.",
                    self._admin_menu_markup(),
                )
                return
            await self._execute_admin_withdraw_approve(
                context=context,
                query_message=query_message,
                admin_user_id=admin_user_id,
                request_id=int(payload.entity_id),
            )
            return
        if action == "withdrawal_reject_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить заявку. Откройте список и попробуйте снова.",
                    self._admin_menu_markup(),
                )
                return
            request_id = int(payload.entity_id)
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_withdraw_reject_reason",
                sensitive=False,
                extra={"request_id": request_id, "admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                f"Введите причину отклонения для заявки #{request_id}.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к выводам",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "withdrawal_sent_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить заявку. Откройте список и попробуйте снова.",
                    self._admin_menu_markup(),
                )
                return
            request_id = int(payload.entity_id)
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_withdraw_tx_hash",
                sensitive=False,
                extra={"request_id": request_id, "admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                f"Введите хэш перевода для заявки #{request_id}.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к выводам",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "prompt_request_id":
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_request_id",
                sensitive=False,
                extra={"admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                "Введите номер заявки на вывод следующим сообщением.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к выводам",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "manual_deposit_prompt":
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_manual_deposit",
                sensitive=False,
                extra={"admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                (
                    "Введите пополнение одной строкой:\n"
                    "<telegram_id> <роль> <сумма_usdt> <комментарий_или_ссылка>\n"
                    "Роль: seller | buyer\n"
                    "Примеры:\n"
                    "10002 buyer 1.0 welcome_bonus\n"
                    "10002 buyer 5.000000 tx:0xabc123"
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к пополнениям",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="deposits_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "deposit_exceptions":
            await self._render_admin_deposit_exceptions(query_message=query_message)
            return
        if action == "deposit_attach_prompt":
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_deposit_attach",
                sensitive=False,
                extra={"admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                "Введите: <id_транзакции> <id_счета>.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к исключениям",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="exceptions_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "deposit_cancel_prompt":
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_deposit_cancel",
                sensitive=False,
                extra={"admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                "Введите: <id_счета> <причина>.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к исключениям",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="exceptions_section",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return

        await self._replace_message(
            query_message,
            "Неизвестное действие администратора.",
            self._admin_menu_markup(),
        )

    async def _handle_prompt_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        identity: TelegramIdentity,
        text: str,
        prompt_state: dict[str, Any],
    ) -> None:
        message = update.message
        if message is None:
            return

        prompt_type = str(prompt_state.get("type", ""))
        if prompt_state.get("sensitive"):
            await self._delete_sensitive_message(
                message,
                notify=bool(prompt_state.get("notify_sensitive_delete", True)),
            )

        if prompt_type == "seller_shop_create_token":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            wb_token = text.strip()
            if seller_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    "Не удалось продолжить создание магазина. Откройте раздел «🏪 Магазины» заново."
                )
                return
            if not wb_token:
                await message.reply_text(
                    "Токен не может быть пустым. Повторите ввод.",
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return
            if self._wb_ping_client is None:
                self._clear_prompt(context)
                await message.reply_text("Проверка токена временно недоступна. Попробуйте позже.")
                return

            try:
                ping_result = await self._wb_ping_client.validate_token(wb_token)
            except Exception:
                self._logger.exception(
                    "seller_shop_create_token_validation_failed",
                    seller_user_id=seller_user_id,
                )
                self._clear_prompt(context)
                await message.reply_text(
                    "Не удалось проверить токен. Попробуйте снова через раздел «🏪 Магазины».",
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return
            if not ping_result.valid:
                details = ping_result.message or "неизвестная ошибка"
                await message.reply_text(
                    (
                        "Токен не прошел проверку и не сохранен.\n"
                        f"Причина: {details}\n"
                        "Проверьте, что токен «Базовый», работает в режиме "
                        "«Только для чтения» и у него есть категории "
                        "«Контент», «Статистика», «Вопросы и отзывы», "
                        "затем отправьте его снова."
                    ),
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return

            token_ciphertext = encrypt_token(wb_token, self._settings.token_cipher_key)
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_shop_title_after_token",
                sensitive=False,
                extra={
                    "seller_user_id": seller_user_id,
                    "validated_token_ciphertext": token_ciphertext,
                },
            )
            await message.reply_text(
                (
                    "Токен валиден. Сообщение с токеном удалено в целях безопасности.\n\n"
                    "Шаг 2/2: введите название магазина следующим сообщением.\n"
                    "Название увидят покупатели, поэтому используйте нейтральное и понятное имя "
                    "без брендов и внутренних пометок."
                ),
                reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
            )
            return

        if prompt_type == "seller_shop_title_after_token":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            token_ciphertext = str(prompt_state.get("validated_token_ciphertext", "")).strip()
            if seller_user_id < 1 or not token_ciphertext:
                self._clear_prompt(context)
                await message.reply_text(
                    (
                        "Не удалось продолжить создание магазина. "
                        "Начните заново из раздела «🏪 Магазины»."
                    ),
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return
            try:
                shop = await self._seller_service.create_shop(
                    seller_user_id=seller_user_id,
                    title=text,
                )
                await self._seller_service.save_validated_shop_token(
                    seller_user_id=seller_user_id,
                    shop_id=shop.shop_id,
                    token_ciphertext=token_ciphertext,
                )
            except ValueError:
                await message.reply_text(
                    "Название магазина не может быть пустым. Повторите ввод.",
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return
            except InvalidStateError as exc:
                details = str(exc).strip().lower()
                if "title" in details and ("exists" in details or "unique" in details):
                    error_text = (
                        "Магазин с таким названием уже есть.\n"
                        "Введите другое название."
                    )
                else:
                    error_text = (
                        "Не удалось создать магазин.\n"
                        "Проверьте название и попробуйте еще раз."
                    )
                await message.reply_text(
                    error_text,
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return

            deep_link = f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"
            self._clear_prompt(context)
            await message.reply_text(
                (
                    f"Магазин «{shop.title}» создан.\n"
                    f"Ссылка для покупателей:\n{deep_link}"
                ),
                reply_markup=self._seller_shop_detail_markup(
                    shop_id=shop.shop_id,
                    token_is_valid=True,
                ),
            )
            return

        if prompt_type == "seller_shop_token":
            shop_id = int(prompt_state.get("shop_id", 0))
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            if shop_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    "Не удалось продолжить ввод токена. Откройте карточку магазина заново."
                )
                return
            try:
                response = await self._seller_processor.handle(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                    text=f"/token_set {shop_id} {text}",
                )
            except Exception:
                self._logger.exception(
                    "seller_shop_token_update_failed",
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                )
                self._clear_prompt(context)
                await self._render_seller_shop_details(
                    query_message=message,
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    notice=(
                        "Не удалось проверить или сохранить токен. "
                        "Попробуйте снова через карточку магазина."
                    ),
                )
                return
            self._clear_prompt(context)
            if seller_user_id > 0:
                await self._render_seller_shop_details(
                    query_message=message,
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    notice=response.text,
                )
            else:
                await message.reply_text(response.text)
            return

        if prompt_type == "seller_shop_rename":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            shop_id = int(prompt_state.get("shop_id", 0))
            token_is_valid = bool(prompt_state.get("token_is_valid", False))
            if seller_user_id < 1 or shop_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    "Не удалось продолжить переименование. Откройте магазины заново.",
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
                )
                return
            try:
                shop = await self._seller_service.rename_shop(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    title=text,
                )
            except ValueError:
                await message.reply_text(
                    "Название магазина не может быть пустым. Повторите ввод.",
                    reply_markup=self._seller_shop_detail_markup(
                        shop_id=shop_id,
                        token_is_valid=token_is_valid,
                    ),
                )
                return
            except (NotFoundError, InvalidStateError) as exc:
                details = str(exc).strip().lower()
                if "title" in details and ("exists" in details or "unique" in details):
                    error_text = (
                        "Магазин с таким названием уже существует.\n"
                        "Введите другое название."
                    )
                else:
                    error_text = (
                        "Не удалось переименовать магазин.\n"
                        "Проверьте название и попробуйте еще раз."
                    )
                await message.reply_text(
                    error_text,
                    reply_markup=self._seller_shop_detail_markup(
                        shop_id=shop_id,
                        token_is_valid=token_is_valid,
                    ),
                )
                return
            self._clear_prompt(context)
            deep_link = f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"
            await message.reply_text(
                (
                    f"Магазин переименован: «{shop.title}».\n"
                    f"Новая ссылка для покупателей:\n{deep_link}"
                ),
                reply_markup=self._seller_shop_detail_markup(
                    shop_id=shop_id,
                    token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
                ),
            )
            return

        if prompt_type == "seller_listing_create":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            shop_id = int(prompt_state.get("shop_id", 0))
            shop_title = str(prompt_state.get("shop_title", "магазин"))
            back_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к объявлениям",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listings",
                            ),
                        )
                    ]
                ]
            )
            if seller_user_id < 1 or shop_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    (
                        "Не удалось продолжить создание объявления. "
                        "Откройте раздел «📦 Объявления» заново."
                    ),
                    reply_markup=back_markup,
                )
                return
            try:
                tokens = shlex.split(text)
            except ValueError:
                tokens = []
            if len(tokens) != 4:
                await message.reply_text(
                    self._listing_create_instruction_text(shop_title=shop_title),
                    reply_markup=back_markup,
                    parse_mode="HTML",
                )
                return
            try:
                wb_product_id = int(tokens[0])
                cashback_rub = Decimal(tokens[1])
                slots = int(tokens[2])
                search_phrase = tokens[3].strip()
                if wb_product_id < 1:
                    raise ValueError("wb_product_id must be >= 1")
                if cashback_rub <= Decimal("0"):
                    raise ValueError("cashback_rub must be > 0")
                if slots < 1:
                    raise ValueError("slots must be >= 1")
                if not search_phrase:
                    raise ValueError("search_phrase must not be empty")
                await self._refresh_display_rub_per_usdt()
                fx_rate = self._display_rub_per_usdt
                reward_usdt = (cashback_rub / fx_rate).quantize(
                    _USDT_EXACT_QUANT,
                    rounding=ROUND_HALF_UP,
                )
                if reward_usdt <= Decimal("0"):
                    raise ValueError("reward_usdt must be > 0")
                snapshot = await self._load_listing_creation_snapshot(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                )
                observed_buyer_price = await self._lookup_listing_buyer_price(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                )
                suggested_display_title = self._sanitize_buyer_display_title(
                    wb_product_id=wb_product_id,
                    source_title=snapshot.name,
                    brand_name=snapshot.brand,
                )
            except (ValueError, InvalidOperation):
                await message.reply_text(
                    (
                        "Не удалось разобрать данные.\n"
                        "Проверьте формат и отправьте строку еще раз."
                    ),
                    reply_markup=back_markup,
                )
                return
            except ListingValidationError as exc:
                await message.reply_text(str(exc), reply_markup=back_markup)
                return
            except (NotFoundError, InvalidStateError, InsufficientFundsError):
                await message.reply_text(
                    (
                        "Не удалось создать объявление.\n"
                        "Проверьте токен магазина, баланс и введенные значения."
                    ),
                    reply_markup=back_markup,
                )
                return

            next_prompt_type = "seller_listing_create_review"
            prompt_reply_text = self._listing_title_confirmation_text(
                wb_product_id=wb_product_id,
                search_phrase=search_phrase,
                cashback_rub=cashback_rub,
                slot_count=slots,
                snapshot=snapshot,
                suggested_display_title=suggested_display_title,
                buyer_price_rub=(
                    observed_buyer_price.buyer_price_rub if observed_buyer_price is not None else 0
                ),
                reference_price_source="orders" if observed_buyer_price is not None else "manual",
                observed_buyer_price=observed_buyer_price,
            )
            if observed_buyer_price is None:
                next_prompt_type = "seller_listing_manual_price"
                prompt_reply_text = self._listing_manual_price_prompt_text(
                    wb_product_id=wb_product_id,
                    snapshot=snapshot,
                )

            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type=next_prompt_type,
                sensitive=False,
                extra={
                    "seller_user_id": seller_user_id,
                    "shop_id": shop_id,
                    "shop_title": shop_title,
                    "wb_product_id": wb_product_id,
                    "cashback_rub": str(cashback_rub),
                    "reward_usdt": str(reward_usdt),
                    "slot_count": slots,
                    "search_phrase": search_phrase,
                    "wb_source_title": snapshot.name,
                    "wb_subject_name": snapshot.subject_name,
                    "wb_brand_name": snapshot.brand,
                    "wb_vendor_code": snapshot.vendor_code,
                    "wb_description": snapshot.description,
                    "wb_photo_url": snapshot.photo_url,
                    "wb_tech_sizes": snapshot.tech_sizes,
                    "wb_characteristics": snapshot.characteristics,
                    "reference_price_rub": (
                        observed_buyer_price.buyer_price_rub
                        if observed_buyer_price is not None
                        else None
                    ),
                    "reference_price_source": (
                        "orders" if observed_buyer_price is not None else None
                    ),
                    "reference_price_updated_at": (
                        observed_buyer_price.observed_at.isoformat()
                        if (
                            observed_buyer_price is not None
                            and observed_buyer_price.observed_at is not None
                        )
                        else None
                    ),
                    "seller_price_rub": (
                        observed_buyer_price.seller_price_rub
                        if observed_buyer_price is not None
                        else None
                    ),
                    "spp_percent": (
                        observed_buyer_price.spp_percent
                        if observed_buyer_price is not None
                        else None
                    ),
                    "suggested_display_title": suggested_display_title,
                },
            )
            if next_prompt_type == "seller_listing_create_review":
                await self._reply_with_photo_if_available(
                    message,
                    photo_url=snapshot.photo_url,
                )
                await message.reply_text(
                    prompt_reply_text,
                    reply_markup=self._listing_title_review_markup(),
                    parse_mode="HTML",
                )
            else:
                await message.reply_text(
                    prompt_reply_text,
                    reply_markup=back_markup,
                    parse_mode="HTML",
                )
            return

        if prompt_type == "seller_listing_manual_price":
            back_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к объявлениям",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listings",
                            ),
                        )
                    ]
                ]
            )
            try:
                buyer_price_rub = int(
                    Decimal(text.strip()).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            except (InvalidOperation, ValueError):
                await message.reply_text(
                    "Неверный формат цены. Введите сумму в рублях, например 392.",
                    reply_markup=back_markup,
                )
                return
            if buyer_price_rub < 1:
                await message.reply_text(
                    "Цена должна быть больше 0.",
                    reply_markup=back_markup,
                )
                return
            prompt_state["reference_price_rub"] = buyer_price_rub
            prompt_state["reference_price_source"] = "manual"
            prompt_state["reference_price_updated_at"] = datetime.now(UTC).isoformat()
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_listing_create_review",
                sensitive=False,
                extra={
                    key: value
                    for key, value in prompt_state.items()
                    if key not in {"role", "type", "sensitive"}
                },
            )
            await self._render_pending_listing_title_review(
                query_message=message,
                prompt_state=context.user_data[_PROMPT_STATE_KEY],
            )
            return

        if prompt_type == "seller_listing_title_edit":
            suggested_display_title = text.strip()
            back_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к объявлениям",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="listings",
                            ),
                        )
                    ]
                ]
            )
            if not suggested_display_title:
                await message.reply_text(
                    "Название для покупателей не может быть пустым. Отправьте новый текст.",
                    reply_markup=back_markup,
                )
                return
            self._set_prompt(
                context,
                role=_ROLE_SELLER,
                prompt_type="seller_listing_create_review",
                sensitive=False,
                extra={
                    key: value
                    for key, value in prompt_state.items()
                    if key not in {"role", "type", "sensitive"}
                },
            )
            context.user_data[_PROMPT_STATE_KEY]["suggested_display_title"] = (
                suggested_display_title
            )
            await self._render_pending_listing_title_review(
                query_message=message,
                prompt_state=context.user_data[_PROMPT_STATE_KEY],
            )
            return

        if prompt_type == "seller_listing_create_review":
            await message.reply_text(
                "Используйте кнопки ниже, чтобы сохранить или изменить название.",
                reply_markup=self._listing_title_review_markup(),
            )
            return

        if prompt_type in {"seller_listing_edit_value", "seller_listing_edit_confirm"}:
            self._clear_prompt(context)
            await message.reply_text(
                self._screen_text(
                    title="Редактирование отключено",
                    lines=[
                        "Изменение объявления недоступно.",
                    ],
                    note="Создайте новое объявление с нужными параметрами и удалите старое.",
                    warning=True,
                ),
                parse_mode="HTML",
            )
            return

        if prompt_type == "seller_topup_amount":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            if seller_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    "Не удалось продолжить пополнение. Откройте раздел «💰 Баланс» заново.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return
            try:
                amount = Decimal(text)
            except InvalidOperation:
                await message.reply_text(
                    "Неверный формат суммы. Введите число, например 1.2.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return
            if amount <= Decimal("0.000000"):
                await message.reply_text(
                    "Сумма должна быть больше 0.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return

            shards = await self._deposit_service.list_active_shards()
            if not shards:
                await message.reply_text(
                    "Адрес для оплаты временно недоступен. Попробуйте позже.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return
            target_shard = next(
                (
                    shard
                    for shard in shards
                    if shard.shard_key == self._settings.seller_collateral_shard_key
                ),
                shards[0],
            )
            try:
                intent = await self._deposit_service.create_seller_deposit_intent(
                    seller_user_id=seller_user_id,
                    request_amount_usdt=amount,
                    shard_id=target_shard.shard_id,
                    idempotency_key=f"tg-seller-topup:{seller_user_id}:{update.update_id}",
                )
            except (NotFoundError, InvalidStateError, ValueError) as exc:
                details = str(exc).strip()
                if "all 999 suffixes" in details:
                    await message.reply_text(
                        (
                            "Сейчас нельзя создать новый счет: достигнут лимит "
                            "активных счетов.\nПопробуйте позже."
                        ),
                        reply_markup=self._seller_balance_menu_markup(),
                    )
                    return
                await message.reply_text(
                    "Не удалось создать счет на пополнение. Попробуйте еще раз.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return
            except Exception:
                self._logger.exception(
                    "seller_topup_intent_create_failed",
                    seller_user_id=seller_user_id,
                    telegram_update_id=update.update_id,
                )
                await message.reply_text(
                    "Техническая ошибка при создании счета. Попробуйте еще раз.",
                    reply_markup=self._seller_balance_menu_markup(),
                )
                return
            self._clear_prompt(context)
            expected_amount_text = self._format_copyable_code(
                f"{self._format_usdt_value(intent.expected_amount_usdt, precise=True)} USDT"
            )
            await message.reply_text(
                self._screen_text(
                    title="Счет на пополнение создан",
                    cta=(
                        "Откройте Телеграм Кошелек или используйте ссылку для других "
                        "кошельков, либо скопируйте адрес и сумму вручную."
                    ),
                    lines=[
                        (
                            "<b>Срок действия:</b> "
                            f"{self._settings.seller_collateral_invoice_ttl_hours} ч"
                        ),
                        "<b>Сеть:</b> USDT в сети TON (не ERC-20)",
                        f"<b>Адрес:</b> {self._format_copyable_code(intent.deposit_address)}",
                        (
                            "<b>Сумма (должна полностью совпадать):</b> "
                            f"{expected_amount_text}"
                        ),
                    ],
                    note=(
                        "Телеграм Кошелек откроется без автоматически подставленного перевода. "
                        "Ссылка для других кошельков может открыть уже подготовленный перевод. "
                        "В любом случае адрес и сумму можно скопировать вручную."
                    ),
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="👛 Открыть Телеграм Кошелек",
                                url=self._build_telegram_wallet_open_link(),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔗 Ссылка (другие кошельки)",
                                url=self._build_ton_usdt_wallet_link(
                                    destination_address=intent.deposit_address,
                                    expected_amount_usdt=intent.expected_amount_usdt,
                                    text=f"QPI deposit #{intent.deposit_intent_id}",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❓ Как перевести?",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="topup_help",
                                ),
                            )
                        ],
                        *self._seller_balance_menu_markup().inline_keyboard,
                    ]
                ),
                parse_mode="HTML",
            )
            return

        if prompt_type == "buyer_shop_slug":
            self._clear_prompt(context)
            context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = text
            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            await self._send_buyer_shop_catalog(
                message,
                slug=text,
                buyer_user_id=buyer.user_id,
            )
            return

        if prompt_type == "buyer_submit_payload":
            assignment_id = int(prompt_state.get("assignment_id", 0))
            if assignment_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Задание не найдено. Откройте список заданий заново.")
                return
            try:
                buyer = await self._buyer_service.bootstrap_buyer(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                )
                result = await self._buyer_service.submit_purchase_payload(
                    buyer_user_id=buyer.user_id,
                    assignment_id=assignment_id,
                    payload_base64=text,
                )
            except NotFoundError:
                await message.reply_text("Задание не найдено.")
                return
            except PayloadValidationError as exc:
                details = str(exc).strip().lower()
                base = (
                    "Токен-подтверждение не принят.\n"
                    "Проверьте, что вы скопировали его полностью из расширения для этого задания."
                )
                if details and "timezone" in details:
                    await message.reply_text(
                        f"{base}\nПроверьте дату и время на устройстве и сформируйте токен заново."
                    )
                else:
                    await message.reply_text(base)
                return
            except DuplicateOrderError:
                await message.reply_text("Этот номер заказа уже использован в другом задании.")
                return
            except InvalidStateError:
                await message.reply_text(
                    "Сейчас нельзя отправить токен-подтверждение для этого задания."
                )
                return

            self._clear_prompt(context)
            if result.changed:
                reply = (
                    "Токен-подтверждение принят.\n"
                    f"Номер заказа: {result.order_id}\n"
                    "Дальше мы автоматически проверим выкуп и начисление кэшбэка."
                )
            else:
                reply = (
                    "Этот токен-подтверждение уже отправлен ранее.\n"
                    f"Номер заказа: {result.order_id}"
                )
            self._logger.info(
                "buyer_payload_submitted",
                telegram_update_id=update.update_id,
                assignment_id=result.assignment_id,
                changed=result.changed,
            )
            await message.reply_text(reply, reply_markup=self._buyer_menu_markup())
            return

        if prompt_type == "buyer_withdraw_amount":
            buyer_user_id = int(prompt_state.get("buyer_user_id", 0))
            if buyer_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста вывода. Откройте баланс заново.")
                return
            try:
                amount = Decimal(text)
            except InvalidOperation:
                await message.reply_text("Неверный формат суммы. Повторите ввод.")
                return
            if amount <= Decimal("0.000000"):
                await message.reply_text("Сумма должна быть больше 0.")
                return

            self._set_prompt(
                context,
                role=_ROLE_BUYER,
                prompt_type="buyer_withdraw_address",
                sensitive=True,
                extra={"buyer_user_id": buyer_user_id, "amount_usdt": str(amount)},
            )
            await message.reply_text(
                (
                    "Введите адрес кошелька для вывода "
                    f"{self._format_usdt_value(amount, precise=True)} USDT."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к балансу",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="balance",
                                ),
                            )
                        ]
                    ]
                ),
            )
            return

        if prompt_type == "buyer_withdraw_address":
            buyer_user_id = int(prompt_state.get("buyer_user_id", 0))
            amount_raw = str(prompt_state.get("amount_usdt", "0"))
            try:
                amount = Decimal(amount_raw)
            except InvalidOperation:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста суммы. Откройте баланс заново.")
                return
            payout_address = text.strip()
            if not payout_address:
                await message.reply_text("Адрес не может быть пустым. Повторите ввод.")
                return
            if buyer_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста пользователя. Откройте баланс заново.")
                return

            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            if buyer.user_id != buyer_user_id:
                self._clear_prompt(context)
                await message.reply_text("Контекст вывода устарел. Откройте баланс заново.")
                return

            try:
                withdrawal = await self._finance_service.create_withdrawal_request(
                    buyer_user_id=buyer.user_id,
                    from_account_id=buyer.buyer_available_account_id,
                    pending_account_id=buyer.buyer_withdraw_pending_account_id,
                    amount_usdt=amount,
                    payout_address=payout_address,
                    idempotency_key=f"tg-withdraw:{buyer.user_id}:{update.update_id}",
                )
            except InsufficientFundsError:
                await message.reply_text(
                    "Недостаточно доступного баланса для вывода.",
                    reply_markup=self._buyer_menu_markup(),
                )
                return
            except InvalidStateError:
                await message.reply_text(
                    "Не удалось создать заявку на вывод. "
                    "Проверьте доступный баланс и попробуйте снова.",
                    reply_markup=self._buyer_menu_markup(),
                )
                return

            self._clear_prompt(context)
            if withdrawal.created:
                reply = (
                    "Заявка на вывод создана.\n"
                    "Статус: на проверке у администратора."
                )
            else:
                reply = "У вас уже есть активная заявка на вывод."
            self._logger.info(
                "buyer_withdraw_requested",
                telegram_update_id=update.update_id,
                withdrawal_request_id=withdrawal.withdrawal_request_id,
            )
            await message.reply_text(reply, reply_markup=self._buyer_menu_markup())
            return

        if prompt_type == "admin_request_id":
            request_id_raw = text.strip()
            if not request_id_raw.isdigit():
                await message.reply_text("ID заявки должен быть числом.")
                return
            self._clear_prompt(context)
            await self._render_admin_withdrawal_detail(
                query_message=message,
                request_id=int(request_id_raw),
            )
            return

        if prompt_type == "admin_withdraw_reject_reason":
            request_id = int(prompt_state.get("request_id", 0))
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            if request_id < 1 or admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста отклонения. Откройте заявку заново.")
                return
            reason = text.strip()
            if not reason:
                await message.reply_text("Причина отклонения не может быть пустой.")
                return
            self._clear_prompt(context)
            await self._execute_admin_withdraw_reject(
                context=context,
                query_message=message,
                admin_user_id=admin_user_id,
                request_id=request_id,
                reason=reason,
            )
            return

        if prompt_type == "admin_withdraw_tx_hash":
            request_id = int(prompt_state.get("request_id", 0))
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            if request_id < 1 or admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста отправки. Откройте заявку заново.")
                return
            tx_hash = text.strip()
            if not tx_hash:
                await message.reply_text("Хэш перевода не может быть пустым.")
                return
            self._clear_prompt(context)
            await self._execute_admin_withdraw_sent(
                context=context,
                query_message=message,
                admin_user_id=admin_user_id,
                request_id=request_id,
                tx_hash=tx_hash,
            )
            return

        if prompt_type == "admin_manual_deposit":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=3)
            if len(tokens) != 4:
                await message.reply_text(
                    "Формат:\n"
                    "<telegram_id> <роль> <сумма_usdt> <комментарий_или_ссылка>"
                )
                return
            telegram_id_raw, account_kind, amount_raw, external_reference = tokens
            if not telegram_id_raw.isdigit():
                await message.reply_text("Telegram ID должен быть числом.")
                return
            if admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста админа. Откройте меню заново.")
                return
            try:
                amount = Decimal(amount_raw)
            except InvalidOperation:
                await message.reply_text("Неверный формат суммы. Пример: 1.0")
                return
            if amount <= Decimal("0.000000"):
                await message.reply_text("Сумма должна быть больше 0.")
                return

            self._clear_prompt(context)
            await self._execute_admin_manual_deposit(
                context=context,
                query_message=message,
                admin_user_id=admin_user_id,
                target_telegram_id=int(telegram_id_raw),
                account_kind=account_kind,
                amount_usdt=amount,
                external_reference=external_reference,
            )
            return

        if prompt_type == "admin_deposit_attach":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=1)
            if len(tokens) != 2:
                await message.reply_text("Формат: <id_транзакции> <id_счета>")
                return
            chain_tx_raw, intent_raw = tokens
            if not chain_tx_raw.isdigit() or not intent_raw.isdigit():
                await message.reply_text("Оба значения должны быть числами.")
                return
            if admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста админа. Откройте меню заново.")
                return
            self._clear_prompt(context)
            await self._execute_admin_deposit_attach(
                query_message=message,
                admin_user_id=admin_user_id,
                chain_tx_id=int(chain_tx_raw),
                deposit_intent_id=int(intent_raw),
            )
            return

        if prompt_type == "admin_deposit_cancel":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=1)
            if len(tokens) != 2:
                await message.reply_text("Формат: <id_счета> <причина>")
                return
            intent_raw, reason = tokens
            if not intent_raw.isdigit():
                await message.reply_text("ID счета должен быть числом.")
                return
            if not reason.strip():
                await message.reply_text("Причина не может быть пустой.")
                return
            if admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста админа. Откройте меню заново.")
                return
            self._clear_prompt(context)
            await self._execute_admin_deposit_cancel(
                query_message=message,
                admin_user_id=admin_user_id,
                deposit_intent_id=int(intent_raw),
                reason=reason,
            )
            return

        self._clear_prompt(context)
        await message.reply_text("Неизвестный тип ввода. Отправьте /start.")

    async def _send_buyer_shop_catalog(
        self,
        message: Message | None,
        *,
        slug: str,
        buyer_user_id: int | None = None,
        prefer_edit: bool = False,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        try:
            shop = await self._buyer_service.resolve_shop_by_slug(slug=slug)
            listings = await self._buyer_service.list_active_listings_by_shop_slug(
                slug=slug,
                buyer_user_id=buyer_user_id,
            )
        except (NotFoundError, InvalidStateError):
            if prefer_edit:
                await self._replace_message(
                    message,
                    "Магазин недоступен. Проверьте ссылку и попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к магазинам",
                                    callback_data=build_callback(
                                        flow=_ROLE_BUYER,
                                        action="shops",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
            elif message is not None:
                await message.reply_text("Магазин недоступен. Проверьте ссылку и попробуйте снова.")
            return

        if buyer_user_id is not None:
            try:
                await self._buyer_service.touch_saved_shop(
                    buyer_user_id=buyer_user_id,
                    shop_id=shop.shop_id,
                )
            except DomainError:
                self._logger.warning(
                    "buyer_saved_shop_touch_failed",
                    buyer_user_id=buyer_user_id,
                    shop_id=shop.shop_id,
                    slug=shop.slug,
                )

        header = f"Магазин: {shop.title}"
        if not listings:
            text = self._screen_text(
                title=html.escape(header),
                lines=["Активных объявлений пока нет."],
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к магазинам",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                        )
                    ]
                ]
            )
            if prefer_edit:
                await self._replace_message(message, text, markup, parse_mode="HTML")
            elif message is not None:
                await message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return

        lines = [f"<b>{html.escape(header)}</b>", "Активные объявления:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for idx, listing in enumerate(listings, start=1):
            display_title = self._listing_display_title(
                display_title=listing.display_title,
                fallback=listing.search_phrase,
            )
            cashback_text = self._format_cashback_with_percent(
                reward_usdt=listing.reward_usdt,
                reference_price_rub=listing.reference_price_rub,
            )
            lines.append(
                f"<b>Объявление {idx}</b>\n"
                f"Товар: {html.escape(display_title)}\n"
                f"Цена: {self._format_price_optional_rub(listing.reference_price_rub)}\n"
                f"Кэшбэк: {cashback_text}"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="🔎 Просмотр",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="listing_open",
                            entity_id=str(listing.listing_id),
                        ),
                    ),
                    InlineKeyboardButton(
                        text="✅ Выполнить задание",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="reserve",
                            entity_id=str(listing.listing_id),
                        ),
                    )
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к магазинам",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                )
            ]
        )
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard_rows)
        if prefer_edit:
            await self._replace_message(message, text, markup, parse_mode="HTML")
        elif message is not None:
            await message.reply_text(text, reply_markup=markup, parse_mode="HTML")

    async def _dispatch_legacy_command(
        self,
        *,
        telegram_id: int,
        username: str | None,
        raw_text: str,
    ):
        command = raw_text.split(" ", 1)[0].lower()
        if command == "/start":
            return None

        if command.startswith(_SELLER_COMMAND_PREFIXES):
            return await self._seller_processor.handle(
                telegram_id=telegram_id,
                username=username,
                text=raw_text,
            )
        if command.startswith(_BUYER_COMMAND_PREFIXES):
            return await self._buyer_processor.handle(
                telegram_id=telegram_id,
                username=username,
                text=raw_text,
            )
        return None

    async def _delete_sensitive_message(self, message: Message, *, notify: bool = True) -> None:
        deleted = False
        try:
            await message.delete()
            deleted = True
        except Exception as exc:
            self._logger.warning(
                "telegram_sensitive_delete_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
        if notify:
            await message.chat.send_message(
                "Сообщение с чувствительными данными "
                f"{'удалено' if deleted else 'не удалось удалить автоматически'}. "
                "При необходимости удалите его вручную."
            )

    @staticmethod
    def _is_valid_shop_token(status: str | None) -> bool:
        return (status or "").strip().lower() == "valid"

    @staticmethod
    def _format_decimal(
        amount: Decimal,
        *,
        quant: Decimal,
        rounding=ROUND_HALF_UP,
    ) -> str:
        normalized = amount.quantize(quant, rounding=rounding)
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def _format_usdt(self, amount: Decimal, *, precise: bool = False) -> str:
        if precise:
            return f"${self._format_decimal(amount, quant=_USDT_EXACT_QUANT)}"
        normalized = amount.quantize(_USDT_SUMMARY_QUANT, rounding=ROUND_HALF_UP)
        return f"${normalized:.1f}"

    def _format_usdt_value(self, amount: Decimal, *, precise: bool = False) -> str:
        quant = _USDT_EXACT_QUANT if precise else _USDT_SUMMARY_QUANT
        return self._format_decimal(amount, quant=quant)

    def _format_rub_approx(self, amount: Decimal) -> str:
        rub = amount * self._display_rub_per_usdt
        return f"~{self._format_decimal(rub, quant=_RUB_QUANT)} ₽"

    def _format_usdt_with_rub(self, amount: Decimal, *, precise: bool = False) -> str:
        usdt = self._format_usdt(amount, precise=precise)
        if amount.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
            return usdt
        return f"{usdt} ({self._format_rub_approx(amount)})"

    def _format_buyer_listing_cashback(self, amount: Decimal) -> str:
        return self._format_cashback_with_percent(reward_usdt=amount, reference_price_rub=None)

    def _listing_display_title(self, *, display_title: str | None, fallback: str) -> str:
        normalized = (display_title or "").strip()
        return normalized or fallback.strip()

    @staticmethod
    def _normalize_match_text(value: str | None) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", value).strip().lower()

    def _contains_brand_reference(self, *, text: str, brand_name: str | None) -> bool:
        normalized_brand = self._normalize_match_text(brand_name)
        normalized_text = self._normalize_match_text(text)
        if not normalized_brand or not normalized_text:
            return False
        return normalized_brand in normalized_text

    def _sanitize_buyer_display_title(
        self,
        *,
        wb_product_id: int,
        source_title: str,
        brand_name: str | None,
    ) -> str:
        title = source_title.strip()
        brand = (brand_name or "").strip()
        if brand:
            title = re.sub(re.escape(brand), "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s{2,}", " ", title).strip(" -|,;:/")
        if not title or self._contains_brand_reference(text=title, brand_name=brand):
            return f"Товар {wb_product_id}"
        return title

    def _format_cashback_rub_value(self, amount: Decimal) -> str:
        return self._format_decimal(amount * self._display_rub_per_usdt, quant=_RUB_QUANT)

    def _format_cashback_rub_with_percent(
        self,
        *,
        reward_usdt: Decimal,
        reference_price_rub: int | None,
    ) -> str:
        return self._format_cashback_with_percent(
            reward_usdt=reward_usdt,
            reference_price_rub=reference_price_rub,
        )

    def _format_cashback_with_percent(
        self,
        *,
        reward_usdt: Decimal,
        reference_price_rub: int | None,
    ) -> str:
        primary = self._format_usdt_with_rub(reward_usdt)
        if reward_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
            return primary
        if reference_price_rub is None or reference_price_rub < 1:
            return primary
        cashback_rub = Decimal(self._format_cashback_rub_value(reward_usdt))
        percent = (
            cashback_rub / Decimal(reference_price_rub) * Decimal("100")
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"{primary[:-1]}, ~{percent}%)" if primary.endswith(")") else primary

    def _format_price_rub(self, amount: int | Decimal | None) -> str:
        if amount is None:
            return "0 ₽"
        rub = Decimal(str(amount)).quantize(_RUB_QUANT, rounding=ROUND_CEILING)
        return f"{self._format_decimal(rub, quant=_RUB_QUANT)} ₽"

    def _format_price_optional_rub(self, amount: int | Decimal | None) -> str:
        if amount is None:
            return "—"
        return self._format_price_rub(amount)

    @staticmethod
    def _screen_text(
        *,
        title: str,
        cta: str | None = None,
        lines: list[str] | None = None,
        note: str | None = None,
        warning: bool = False,
        separate_blocks: bool = False,
    ) -> str:
        parts = [f"{'⚠️ ' if warning else ''}<b>{title}</b>"]
        if cta:
            parts.append(f"<i>{cta}</i>")
        if lines:
            filtered = [line for line in lines if line]
            if filtered:
                parts.append(("\n\n" if separate_blocks else "\n").join(filtered))
        if note:
            parts.append(f"<i>{note}</i>")
        return "\n\n".join(parts)

    @staticmethod
    def _format_datetime_msk(value: datetime | None) -> str:
        if value is None:
            return "—"
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        localized = normalized.astimezone(_MSK_TZ)
        return localized.strftime("%d.%m.%Y %H:%M MSK")

    @staticmethod
    def _status_badge(label: str, *, color: str) -> str:
        marker = {
            "green": "🟢",
            "red": "🔴",
            "yellow": "🟡",
            "blue": "🔵",
        }.get(color, "⚪")
        return f"{marker} {html.escape(label)}"

    @staticmethod
    def _format_copyable_code(value: str) -> str:
        return f"<code>{html.escape(value.strip())}</code>"

    def _build_ton_usdt_wallet_link(
        self,
        *,
        destination_address: str,
        expected_amount_usdt: Decimal,
        text: str | None = None,
    ) -> str:
        normalized_address = destination_address.strip()
        base_units = int(
            expected_amount_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
            * Decimal("1000000")
        )
        params = {
            "jetton": self._settings.tonapi_usdt_jetton_master,
            "amount": str(base_units),
        }
        if text:
            params["text"] = text.strip()
        query = urllib.parse.urlencode(params)
        encoded_address = urllib.parse.quote(normalized_address, safe="")
        return f"ton://transfer/{encoded_address}?{query}"

    def _build_telegram_wallet_open_link(self) -> str:
        return self._settings.telegram_wallet_open_url

    def _build_buyer_listing_token(self, *, search_phrase: str, wb_product_id: int) -> str:
        payload = [search_phrase, wb_product_id, _BUYER_TASK_COMPANION_PRODUCTS]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _buyer_task_instruction_text(self, assignment) -> str:
        listing_token = self._build_buyer_listing_token(
            search_phrase=assignment.search_phrase,
            wb_product_id=assignment.wb_product_id,
        )
        display_title = self._listing_display_title(
            display_title=getattr(assignment, "display_title", None),
            fallback=assignment.search_phrase,
        )
        return (
            f"<b>Товар:</b> {html.escape(display_title)}\n"
            f"<b>Поисковая фраза:</b> &quot;{html.escape(assignment.search_phrase)}&quot;\n"
            "1. Введите следующий токен в расширении Qpilka:\n"
            f"<code>{listing_token}</code>\n"
            "2. Выполните шаги до оформления заказа.\n"
            "3. Отправьте токен-подтверждение сюда."
        )

    @staticmethod
    def _humanize_listing_status(status: str) -> str:
        mapping = {
            "draft": "Черновик",
            "active": "Активно",
            "paused": "На паузе",
        }
        return mapping.get(status, status)

    def _listing_activity_badge(self, *, is_active: bool) -> str:
        return self._status_badge(
            "активно" if is_active else "не активно",
            color="green" if is_active else "red",
        )

    def _deposit_status_badge(self, status: str) -> str:
        label = self._humanize_deposit_status(status)
        color = {
            "credited": "green",
            "expired": "red",
            "cancelled": "red",
            "manual_review": "yellow",
            "matched": "blue",
            "pending": "yellow",
        }.get(status, "blue")
        return self._status_badge(label, color=color)

    def _withdraw_status_badge(self, status: str) -> str:
        label = self._humanize_withdraw_status(status)
        color = {
            "withdraw_sent": "green",
            "approved": "blue",
            "rejected": "red",
            "withdraw_pending_admin": "yellow",
        }.get(status, "blue")
        return self._status_badge(label, color=color)

    @staticmethod
    def _humanize_assignment_status(status: str) -> str:
        mapping = {
            "reserved": "Ожидает подтверждение покупки",
            "order_submitted": "Подтверждение получено",
            "order_verified": "Покупка проверена",
            "picked_up_wait_unlock": "Ожидаем срок разблокировки кэшбэка",
            "eligible_for_withdrawal": "Кэшбэк доступен для вывода",
            "withdraw_pending_admin": "Заявка на вывод на проверке",
            "withdraw_sent": "Кэшбэк выплачен",
            "expired_2h": "Бронь истекла",
            "wb_invalid": "Проверка WB не пройдена",
            "returned_within_14d": "Заказ возвращен",
            "delivery_expired": "Срок выкупа истек",
        }
        return mapping.get(status, status)

    @staticmethod
    def _humanize_withdraw_status(status: str) -> str:
        mapping = {
            "withdraw_pending_admin": "На проверке",
            "approved": "Одобрено",
            "rejected": "Отклонено",
            "withdraw_sent": "Отправлено",
        }
        return mapping.get(status, status)

    @staticmethod
    def _humanize_deposit_status(status: str) -> str:
        mapping = {
            "pending": "Ожидается оплата",
            "matched": "Платеж найден, идет проверка",
            "manual_review": "Нужна проверка администратором",
            "credited": "Зачислено",
            "expired": "Срок счета истек",
            "cancelled": "Отменено",
        }
        return mapping.get(status, status)

    async def _refresh_display_rub_per_usdt(self) -> None:
        service = self._fx_rate_service
        if service is None:
            return
        try:
            rate = await service.get_usdt_rub_rate(
                max_age_seconds=self._settings.fx_rate_ttl_seconds,
                fallback_rate=self._settings.display_rub_per_usdt,
            )
        except Exception as exc:
            self._logger.warning(
                "fx_rate_refresh_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
            return
        self._display_rub_per_usdt = rate

    async def _load_listing_creation_snapshot(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbProductSnapshot:
        client = self._wb_public_client
        if client is None:
            raise ListingValidationError("Проверка WB временно недоступна. Попробуйте позже.")
        token = await self._load_shop_wb_token(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
        )
        try:
            return await client.fetch_product_snapshot(
                token=token,
                wb_product_id=wb_product_id,
            )
        except WbPublicApiError as exc:
            raise ListingValidationError(
                "Не удалось получить данные о товаре WB. Проверьте артикул и попробуйте еще раз."
            ) from exc

    async def _validate_listing_product_availability(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbProductSnapshot:
        client = self._wb_public_client
        if client is None:
            raise ListingValidationError("Проверка WB временно недоступна. Попробуйте позже.")
        token = await self._load_shop_wb_token(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
        )
        try:
            return await client.fetch_product_snapshot(
                token=token,
                wb_product_id=wb_product_id,
            )
        except WbPublicApiError as exc:
            raise ListingValidationError(
                "Товар сейчас недоступен на WB или его карточка не читается. Попробуйте позже."
            ) from exc

    async def _lookup_listing_buyer_price(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbObservedBuyerPrice | None:
        client = self._wb_public_client
        if client is None:
            raise ListingValidationError("Проверка WB временно недоступна. Попробуйте позже.")
        token = await self._load_shop_wb_token(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
        )
        try:
            return await client.lookup_buyer_price(
                token=token,
                wb_product_id=wb_product_id,
            )
        except WbPublicApiError as exc:
            raise ListingValidationError(
                "Не удалось получить цену покупателя из WB. Попробуйте еще раз позже."
            ) from exc

    async def _load_shop_wb_token(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
    ) -> str:
        try:
            ciphertext = await self._seller_service.get_validated_shop_token_ciphertext(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
            )
        except NotFoundError as exc:
            raise ListingValidationError("Магазин не найден или уже удален.") from exc
        except InvalidStateError as exc:
            raise ListingValidationError(
                "Токен магазина невалиден. Обновите токен WB API."
            ) from exc
        try:
            return decrypt_token(ciphertext, self._settings.token_cipher_key)
        except Exception as exc:
            raise ListingValidationError(
                "Не удалось прочитать токен магазина. Сохраните его заново."
            ) from exc

    def _set_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        role: str,
        prompt_type: str,
        sensitive: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        state = {
            "role": role,
            "type": prompt_type,
            "sensitive": sensitive,
        }
        if extra:
            state.update(extra)
        context.user_data[_PROMPT_STATE_KEY] = state

    def _clear_prompt(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop(_PROMPT_STATE_KEY, None)

    async def _replace_message(
        self,
        message: Message | None,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if message is None:
            return
        try:
            await message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)
        except Exception:
            await message.chat.send_message(text, reply_markup=markup, parse_mode=parse_mode)

    async def _retire_message_keyboard(self, message: Message | None) -> None:
        if message is None:
            return
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            return

    async def _reply_with_photo_if_available(
        self,
        message: Message | None,
        *,
        photo_url: str | None,
    ) -> None:
        if message is None or not photo_url:
            return
        try:
            await message.reply_photo(photo=photo_url)
        except Exception as exc:
            self._logger.warning(
                "telegram_photo_reply_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                photo_url=photo_url,
            )

    def _seller_listing_detail_markup(
        self,
        *,
        listing_id: int,
        status: str,
        list_page: int,
        can_activate: bool,
    ) -> InlineKeyboardMarkup:
        if status == "draft" and can_activate:
            action_button = InlineKeyboardButton(
                text="✅ Активировать",
                callback_data=build_callback(
                    flow=_ROLE_SELLER,
                    action="listing_activate",
                    entity_id=str(listing_id),
                ),
            )
        elif status == "draft":
            action_button = InlineKeyboardButton(
                text="⛔ Недостаточно средств",
                callback_data=build_callback(
                    flow=_ROLE_SELLER,
                    action="listing_activation_blocked",
                    entity_id=str(listing_id),
                ),
            )
        elif status == "active":
            action_button = InlineKeyboardButton(
                text="⏸ Пауза",
                callback_data=build_callback(
                    flow=_ROLE_SELLER,
                    action="listing_pause",
                    entity_id=str(listing_id),
                ),
            )
        else:
            action_button = InlineKeyboardButton(
                text="▶️ Снять паузу",
                callback_data=build_callback(
                    flow=_ROLE_SELLER,
                    action="listing_unpause",
                    entity_id=str(listing_id),
                ),
            )
        return InlineKeyboardMarkup(
            [
                [
                    action_button,
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_delete_preview",
                            entity_id=str(listing_id),
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад к объявлениям",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listings",
                            entity_id=str(list_page),
                        ),
                    )
                ],
            ]
        )

    @staticmethod
    def _listing_has_sufficient_collateral(collateral_view) -> bool:
        if collateral_view is None:
            return True
        return collateral_view.collateral_locked_usdt >= collateral_view.collateral_required_usdt

    def _format_listing_collateral_line(self, *, collateral_view) -> str:
        if collateral_view is None:
            return "—"
        required_text = self._format_usdt_with_rub(collateral_view.collateral_required_usdt)
        if self._listing_has_sufficient_collateral(collateral_view):
            return f"🟢 {required_text}"
        return (
            "🔴 "
            f"{self._format_usdt(collateral_view.collateral_required_usdt)} "
            "(недостаточно средств)"
        )

    def _listing_detail_note(self, *, listing, collateral_view) -> str:
        if listing.status == "active":
            return (
                "Объявление активно. При необходимости поставьте его на паузу "
                "или поделитесь ссылкой на магазин."
            )
        if not self._listing_has_sufficient_collateral(collateral_view):
            return (
                "Для активации пополните баланс продавца, затем вернитесь "
                "к карточке объявления."
            )
        return "Проверьте параметры и активируйте объявление, когда будете готовы."

    def _seller_listing_detail_html(
        self,
        *,
        listing,
        collateral_view,
        shop_link: str | None = None,
        notice: str | None = None,
    ) -> str:
        display_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        planned = collateral_view.slot_count if collateral_view is not None else listing.slot_count
        in_progress = (
            collateral_view.in_progress_assignments_count if collateral_view is not None else 0
        )
        cashback_text = self._format_cashback_with_percent(
            reward_usdt=listing.reward_usdt,
            reference_price_rub=listing.reference_price_rub,
        )
        is_active = listing.status == "active"
        title = f"{'🟢' if is_active else '🔴'} {html.escape(display_title)}"
        lines: list[str] = []
        if notice:
            lines.append(html.escape(notice))
        lines.extend(
            [
                f"<b>Артикул WB:</b> {listing.wb_product_id}",
                f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
                f"<b>План по заказам / В процессе:</b> {planned} / {in_progress}",
            ]
        )
        if shop_link:
            lines.append(f"<b>Ссылка на магазин:</b>\n{html.escape(shop_link)}")
        lines.extend(
            [
                (
                    "<b>Обеспечение:</b> "
                    f"{self._format_listing_collateral_line(collateral_view=collateral_view)}"
                ),
                (
                    f"<b>Статус:</b> "
                    f"{self._listing_activity_badge(is_active=is_active)}"
                ),
            ]
        )
        parameters_lines = [
            f"Предмет: {html.escape(listing.wb_subject_name or '—')}",
            f"Артикул продавца: {html.escape(listing.wb_vendor_code or '—')}",
            f"Бренд: {html.escape(listing.wb_brand_name or '—')}",
            f"Название WB: {html.escape(listing.wb_source_title or display_title)}",
            self._format_listing_price_line(
                label="Цена покупателя",
                price_rub=listing.reference_price_rub,
                source=listing.reference_price_source,
            ).replace("<b>", "").replace("</b>", ""),
            f"Размеры: {html.escape(self._format_sizes_text(listing.wb_tech_sizes))}",
        ]
        lines.append(
            "\n<b>Параметры</b>\n<blockquote expandable>"
            + "\n".join(parameters_lines)
            + "</blockquote>"
        )
        description_block = self._format_expandable_block_html(
            title="Описание",
            body=listing.wb_description,
        )
        if description_block:
            lines.append(f"\n{description_block}")
        characteristics_block = self._format_characteristics_block_html(listing.wb_characteristics)
        if characteristics_block:
            lines.append(f"\n{characteristics_block}")
        return self._screen_text(
            title=title,
            cta="Проверьте объявление и выберите следующее действие ниже.",
            lines=lines,
            note=self._listing_detail_note(listing=listing, collateral_view=collateral_view),
        )

    def _buyer_listing_detail_html(self, *, listing, notice: str | None = None) -> str:
        display_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        lines: list[str] = []
        if notice:
            lines.append(html.escape(notice))
        cashback_text = self._format_cashback_with_percent(
            reward_usdt=listing.reward_usdt,
            reference_price_rub=listing.reference_price_rub,
        )
        lines.extend(
            [
                f"<b>Артикул WB:</b> {listing.wb_product_id}",
                f"<b>Предмет:</b> {html.escape(listing.wb_subject_name or '—')}",
                f"<b>Бренд:</b> {html.escape(listing.wb_brand_name or '—')}",
                f"<b>Название WB:</b> {html.escape(listing.wb_source_title or display_title)}",
                self._format_listing_price_line(
                    label="Цена",
                    price_rub=listing.reference_price_rub,
                    source=None,
                ),
                f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
                f"<b>Размеры:</b> {html.escape(self._format_sizes_text(listing.wb_tech_sizes))}",
            ]
        )
        description_block = self._format_expandable_block_html(
            title="Описание",
            body=listing.wb_description,
        )
        if description_block:
            lines.append(f"\n{description_block}")
        characteristics_block = self._format_characteristics_block_html(listing.wb_characteristics)
        if characteristics_block:
            lines.append(f"\n{characteristics_block}")
        return self._screen_text(
            title=display_title,
            cta="Проверьте товар и выберите следующее действие ниже.",
            lines=lines,
            note="Если товар подходит, нажмите «Выполнить задание» и следуйте шагам из задания.",
        )

    def _format_listing_price_line(
        self,
        *,
        label: str,
        price_rub: int | None,
        source: str | None,
    ) -> str:
        if price_rub is None:
            return f"<b>{html.escape(label)}:</b> —"
        suffix = ""
        if source == "orders":
            suffix = " (из заказов)"
        elif source == "manual":
            suffix = " (вручную)"
        return (
            f"<b>{html.escape(label)}:</b> "
            f"{self._format_price_rub(price_rub)}{html.escape(suffix)}"
        )

    @staticmethod
    def _format_sizes_text(sizes: list[str] | None) -> str:
        if not sizes:
            return "—"
        return ", ".join(size for size in sizes if size)

    def _format_characteristics_block_html(
        self,
        characteristics: list[dict[str, str]] | None,
    ) -> str | None:
        if not characteristics:
            return None
        lines = []
        for item in characteristics:
            name = html.escape(str(item.get("name", "")).strip())
            value = html.escape(str(item.get("value", "")).strip())
            if not name or not value:
                continue
            lines.append(f"{name}: {value}")
        if not lines:
            return None
        return "<b>Характеристики</b>\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>"

    def _format_expandable_block_html(self, *, title: str, body: str | None) -> str | None:
        normalized = (body or "").strip()
        if not normalized:
            return None
        return (
            f"<b>{html.escape(title)}</b>\n"
            f"<blockquote expandable>{html.escape(normalized)}</blockquote>"
        )

    def _root_menu_markup(self, *, identity: TelegramIdentity | None) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(
                    text="🛍 Я продавец",
                    callback_data=build_callback(
                        flow="root",
                        action="role",
                        entity_id=_ROLE_SELLER,
                    ),
                ),
                InlineKeyboardButton(
                    text="🛒 Я покупатель",
                    callback_data=build_callback(
                        flow="root",
                        action="role",
                        entity_id=_ROLE_BUYER,
                    ),
                ),
            ]
        ]
        is_admin = identity is not None and identity.telegram_id in self._admin_telegram_ids
        if is_admin:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text="🛠 Я админ",
                        callback_data=build_callback(
                            flow="root",
                            action="role",
                            entity_id=_ROLE_ADMIN,
                        ),
                    )
                ]
            )
        return InlineKeyboardMarkup(keyboard)

    def _seller_menu_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="📦 Объявления",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                    ),
                    InlineKeyboardButton(
                        text="🏬 Магазины",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💰 Баланс",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="balance"),
                    ),
                ],
            ]
        )

    def _seller_balance_menu_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Пополнить",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="topup_prompt"),
                    ),
                    InlineKeyboardButton(
                        text="🧾 Транзакции",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="topup_history"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                    )
                ],
            ]
        )

    def _buyer_menu_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="🏪 Магазины",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="shops",
                        ),
                    ),
                    InlineKeyboardButton(
                        text="📋 Задания",
                        callback_data=build_callback(flow=_ROLE_BUYER, action="assignments"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💳 Баланс и вывод",
                        callback_data=build_callback(flow=_ROLE_BUYER, action="balance"),
                    ),
                ],
            ]
        )

    def _admin_menu_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="💸 Выводы",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawals_section",
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🏦 Депозиты",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposits_section",
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⚠️ Исключения",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="exceptions_section",
                        ),
                    ),
                ],
            ]
        )

    async def _handle_error(
        self,
        update: object,
        context: CallbackContext,
    ) -> None:
        error = context.error
        update_id = getattr(update, "update_id", None)
        if isinstance(error, DomainError):
            self._logger.warning(
                "telegram_domain_error",
                update_id=update_id,
                error_type=type(error).__name__,
                error_message=str(error)[:500],
            )
            await self._notify_error_to_user(
                update,
                "Не удалось выполнить действие. Попробуйте еще раз или отправьте /start.",
            )
            return
        self._logger.exception(
            "telegram_update_handler_failed",
            update_id=update_id,
            error_type=type(error).__name__ if error else None,
            error_message=str(error)[:500] if error else None,
        )
        await self._notify_error_to_user(
            update,
            "Произошла ошибка. Попробуйте снова или отправьте /start.",
        )

    async def _notify_error_to_user(self, update: object, text: str) -> None:
        message = getattr(update, "effective_message", None)
        if message is None:
            callback_query = getattr(update, "callback_query", None)
            message = getattr(callback_query, "message", None)
        if message is None:
            message = getattr(update, "message", None)
        if message is None or not hasattr(message, "reply_text"):
            return
        try:
            await message.reply_text(text)
        except Exception as exc:
            self._logger.warning(
                "telegram_error_user_notify_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )

    def _health_payload(self) -> dict[str, Any]:
        payload = {
            "service": "bot_api",
            "ready": self._ready,
            "status": "ok" if self._ready else "starting",
        }
        if self._startup_error:
            payload["status"] = "startup_failed"
            payload["error"] = self._startup_error
        return payload

    async def _assert_runtime_schema_compatibility(self) -> None:
        async with self._db_pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = ANY(%s)
                    """,
                    (list(_RUNTIME_REQUIRED_SCHEMA_COLUMNS.keys()),),
                )
                rows = await cur.fetchall()

        actual_columns: dict[str, set[str]] = {}
        for row in rows:
            actual_columns.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))

        missing_columns: list[str] = []
        for table_name, required_columns in _RUNTIME_REQUIRED_SCHEMA_COLUMNS.items():
            available = actual_columns.get(table_name, set())
            for column_name in sorted(required_columns - available):
                missing_columns.append(f"{table_name}.{column_name}")

        if missing_columns:
            missing_list = ", ".join(missing_columns)
            raise RuntimeError(
                "runtime schema compatibility check failed; missing columns: "
                f"{missing_list}"
            )

        self._logger.info(
            "telegram_runtime_schema_compatibility_ok",
            required_tables=len(_RUNTIME_REQUIRED_SCHEMA_COLUMNS),
            required_columns=sum(
                len(columns) for columns in _RUNTIME_REQUIRED_SCHEMA_COLUMNS.values()
            ),
        )

    def _build_webhook_url(self) -> str:
        if not self._settings.webhook_base_url:
            raise ValueError(
                "WEBHOOK_BASE_URL is required for webhook runtime "
                "(example: https://158.160.187.114:8443)."
            )
        return f"{self._settings.webhook_base_url.rstrip('/')}/{self._settings.webhook_path}"


def _identity_from_update(update: Update) -> TelegramIdentity | None:
    message = update.message
    if message is None or message.from_user is None:
        return None
    from_user = message.from_user
    return TelegramIdentity(telegram_id=from_user.id, username=from_user.username)


def _identity_from_callback(update: Update) -> TelegramIdentity | None:
    callback = update.callback_query
    if callback is None or callback.from_user is None:
        return None
    return TelegramIdentity(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
    )
