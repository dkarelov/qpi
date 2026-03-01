from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    NoSlotsAvailableError,
    NotFoundError,
    PayloadValidationError,
)
from libs.domain.fx_rates import FxRateService
from libs.domain.ledger import FinanceService
from libs.domain.seller import SellerService
from libs.integrations.fx_rates import CoinGeckoUsdtRubClient
from libs.integrations.wb import WbPingClient
from libs.logging.setup import EventLogger, get_logger
from libs.security.token_cipher import encrypt_token
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
_USDT_SUMMARY_QUANT = Decimal("0.1")
_USDT_EXACT_QUANT = Decimal("0.000001")
_RUB_QUANT = Decimal("1")

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
        await self._db_pool.open()
        await self._db_pool.check()

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
                context.user_data[_ACTIVE_ROLE_KEY] = _ROLE_BUYER
                context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = shop_slug
                await self._send_buyer_shop_catalog(update.message, slug=shop_slug)
                await update.message.reply_text(
                    "Выберите действие:",
                    reply_markup=self._buyer_menu_markup(),
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
                (
                    "Шаг 1/2: отправьте токен WB API.\n\n"
                    "Сначала проверим токен и только потом попросим название магазина."
                ),
                self._seller_shops_menu_markup(has_shops=True),
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
                    "Старая ссылка перестанет работать для покупателей.\n\n"
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
            )
            return
        if action == "listings":
            await self._render_seller_listings(
                query_message=query_message,
                seller_user_id=seller.user_id,
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
            await self._replace_message(
                query_message,
                (
                    f"Создание листинга для магазина «{shop.title}».\n\n"
                    "Отправьте одной строкой:\n"
                    "<артикул_WB> <скидка_%> <кэшбэк_USDT> <мест>\n\n"
                    "Пример: 12345678 20 1.5 10"
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к листингам",
                                callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                            )
                        ]
                    ]
                ),
            )
            return
        if action == "listing_activate":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить листинг. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_activate(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "listing_pause":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить листинг. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_pause(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "listing_unpause":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить листинг. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_unpause(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "listing_delete_preview":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить листинг. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._render_listing_delete_preview(
                query_message=query_message,
                seller_user_id=seller.user_id,
                listing_id=int(payload.entity_id),
            )
            return
        if action == "listing_delete_confirm":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось определить листинг. Нажмите кнопку еще раз.",
                    self._seller_menu_markup(),
                )
                return
            await self._execute_listing_delete(
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
            )
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

        text = (
            f"<b>Магазинов:</b> {shops_total} · {shops_active} активных\n"
            f"<b>Листинги:</b> {listings_total} · {listings_active} активных\n"
            "<b>Заказы:</b> "
            f"{orders['in_progress']} в процессе · "
            f"{orders['completed']} оформленных · "
            f"{orders['picked_up']} выкупленных\n"
            f"<b>Баланс:</b> {self._format_usdt_with_rub(balance_total)} · "
            f"{self._format_usdt_with_rub(balance_free)} свободно"
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
            text = "🏬 Магазинов пока нет. Нажмите «➕ Создать магазин»."
            if notice:
                text = f"{notice}\n\n{text}"
            await self._replace_message(
                query_message,
                text,
                self._seller_shops_menu_markup(has_shops=False),
            )
            return

        lines = ["🏬 Ваши магазины. Выберите магазин:"]
        if notice:
            lines.insert(0, notice)
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
            "\n\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
        lines = [f"🏬 Магазин «{shop.title}»", f"🔗 Ссылка для покупателей:\n{deep_link}"]
        if notice:
            lines.insert(0, notice)
        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            self._seller_shop_detail_markup(
                shop_id=shop_id,
                token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
            ),
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
                        text="🧭 Дашборд продавца" if has_shops else "🧭 Назад",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                    )
                ],
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

    def _shop_token_instruction_text(self, *, shop_title: str) -> str:
        return (
            f"🔐 Магазин «{shop_title}»\n"
            "Отправьте сообщением токен WB API.\n\n"
            "Зачем нужен токен?\n"
            "Чтобы бот мог отслеживать статус заказов покупателей, фиксировать момент "
            "выкупа, и контролировать срок, после которого можно разблокировать кэшбэк "
            "покупателю.\n\n"
            "Где найти токен?\n"
            "Войдите в ЛК ВБ > Интеграции по API > Создать токен > Для интеграции вручную "
            "> Базовый токен > Статистика > Только чтение\n\n"
            "Безопасно ли это?\n"
            "Да, токен получает доступ только в режиме чтения и только к категории "
            "\"Статистика\"."
        )

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

        text = (
            f"⚠️ ВНИМАНИЕ: удаление магазина «{shop.title}» необратимо.\n"
            f"Активных листингов: {preview.active_listings_count}\n"
            f"Открытых назначений: {preview.open_assignments_count}\n"
            "После подтверждения:\n"
            f"- связанным назначениям уйдет: {preview.assignment_linked_reserved_usdt} USDT\n"
            f"- продавцу вернется: {preview.unassigned_collateral_usdt} USDT"
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
                f"Переведено покупателям: {result.assignment_transferred_usdt} USDT\n"
                f"Возвращено продавцу: {result.unassigned_collateral_returned_usdt} USDT"
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
        query_message: Message | None,
        seller_user_id: int,
        notice: str | None = None,
    ) -> None:
        listings = await self._seller_service.list_listing_collateral_views(
            seller_user_id=seller_user_id
        )
        if not listings:
            text = "📦 Листинги не найдены. Нажмите «➕ Создать листинг»."
            if notice:
                text = f"{notice}\n\n{text}"
            await self._replace_message(
                query_message,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="➕ Создать листинг",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listing_create_pick_shop",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🧭 Дашборд продавца",
                                callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                            )
                        ],
                    ]
                ),
            )
            return

        shops = await self._seller_service.list_shops(seller_user_id=seller_user_id)
        shop_titles = {shop.shop_id: shop.title for shop in shops}
        lines = ["📦 Ваши листинги:"]
        if notice:
            lines.insert(0, notice)
        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="➕ Создать листинг",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listing_create_pick_shop",
                    ),
                )
            ]
        ]
        for idx, listing in enumerate(listings, start=1):
            shop_title = shop_titles.get(listing.shop_id, "Неизвестный магазин")
            lines.append(
                f"Листинг {idx} · магазин: {shop_title}\n"
                f"Статус: {self._humanize_listing_status(listing.status)}\n"
                f"Кэшбэк: {self._format_usdt_value(listing.reward_usdt, precise=True)} USDT · "
                f"места: {listing.available_slots}/"
                f"{listing.slot_count}\n"
                "Обеспечение: "
                f"{self._format_usdt_value(listing.collateral_locked_usdt, precise=True)}/"
                f"{self._format_usdt_value(listing.collateral_required_usdt, precise=True)} USDT · "
                f"резерв: {self._format_usdt_value(listing.reserved_slot_usdt, precise=True)} USDT"
            )
            action_button: InlineKeyboardButton
            if listing.status == "draft":
                action_button = InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listing_activate",
                        entity_id=str(listing.listing_id),
                    ),
                )
            elif listing.status == "active":
                action_button = InlineKeyboardButton(
                    text="⏸ Пауза",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listing_pause",
                        entity_id=str(listing.listing_id),
                    ),
                )
            else:
                action_button = InlineKeyboardButton(
                    text="▶️ Снять паузу",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listing_unpause",
                        entity_id=str(listing.listing_id),
                    ),
                )
            keyboard_rows.append(
                [
                    action_button,
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="listing_delete_preview",
                            entity_id=str(listing.listing_id),
                        ),
                    ),
                ]
            )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="🧭 Дашборд продавца",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
                    text="↩️ Назад к листингам",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            "Выберите магазин для нового листинга:",
            InlineKeyboardMarkup(keyboard_rows),
        )

    async def _execute_listing_activate(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
    ) -> None:
        try:
            result = await self._seller_service.activate_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                idempotency_key=f"tg-listing-activate:{seller_user_id}:{listing_id}",
            )
        except NotFoundError:
            await self._replace_message(query_message, "Листинг не найден.")
            return
        except InvalidStateError:
            await self._replace_message(
                query_message,
                "Не удалось активировать листинг. Проверьте токен магазина и обеспечение.",
            )
            return
        except InsufficientFundsError:
            await self._replace_message(
                query_message,
                "Недостаточно средств для активации. Откройте «💰 Баланс» -> «➕ Пополнить».",
            )
            return

        if result.changed:
            message = "Листинг активирован."
        else:
            message = "Листинг уже активен."
        self._logger.info("seller_listing_activated", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listings(
            query_message=query_message,
            seller_user_id=seller_user_id,
            notice=message,
        )

    async def _execute_listing_pause(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
    ) -> None:
        try:
            result = await self._seller_service.pause_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
                reason="manual_pause",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(query_message, "Не удалось поставить листинг на паузу.")
            return

        if result.changed:
            message = "Листинг поставлен на паузу."
        else:
            message = "Листинг уже на паузе."
        self._logger.info("seller_listing_paused", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listings(
            query_message=query_message,
            seller_user_id=seller_user_id,
            notice=message,
        )

    async def _execute_listing_unpause(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
    ) -> None:
        try:
            result = await self._seller_service.unpause_listing(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(query_message, "Не удалось снять паузу с листинга.")
            return

        if result.changed:
            message = "Листинг снова активен."
        else:
            message = "Листинг уже активен."
        self._logger.info("seller_listing_unpaused", listing_id=listing_id, changed=result.changed)
        await self._render_seller_listings(
            query_message=query_message,
            seller_user_id=seller_user_id,
            notice=message,
        )

    async def _render_listing_delete_preview(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        listing_id: int,
    ) -> None:
        try:
            preview = await self._seller_service.get_listing_delete_preview(
                seller_user_id=seller_user_id,
                listing_id=listing_id,
            )
        except NotFoundError:
            await self._replace_message(query_message, "Листинг не найден.")
            return

        text = (
            "⚠️ ВНИМАНИЕ: удаление листинга необратимо.\n"
            f"Открытых назначений: {preview.open_assignments_count}\n"
            f"Покупателям уйдет: {preview.assignment_linked_reserved_usdt} USDT\n"
            f"Продавцу вернется: {preview.unassigned_collateral_usdt} USDT"
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
                            callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                        )
                    ],
                ]
            ),
        )

    async def _execute_listing_delete(
        self,
        *,
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
            await self._replace_message(query_message, "Листинг не найден.")
            return

        if not result.changed:
            message = "Листинг уже удален."
        else:
            message = (
                "Листинг удален.\n"
                f"Переведено покупателям: {result.assignment_transferred_usdt} USDT\n"
                f"Возвращено продавцу: {result.unassigned_collateral_returned_usdt} USDT"
            )
        self._logger.info(
            "seller_listing_deleted",
            listing_id=listing_id,
            assignment_transferred_usdt=str(result.assignment_transferred_usdt),
            unassigned_collateral_returned_usdt=str(result.unassigned_collateral_returned_usdt),
        )
        await self._render_seller_listings(
            query_message=query_message,
            seller_user_id=seller_user_id,
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
        locked_total = sum((item.collateral_locked_usdt for item in listings), Decimal("0"))
        required_total = sum((item.collateral_required_usdt for item in listings), Decimal("0"))
        total_balance = snapshot.seller_available_usdt + snapshot.seller_collateral_usdt
        text = (
            "💰 Баланс продавца\n"
            f"Свободно: {self._format_usdt_with_rub(snapshot.seller_available_usdt)}\n"
            f"Обеспечение: {self._format_usdt_with_rub(snapshot.seller_collateral_usdt)}\n"
            f"Итого: {self._format_usdt_with_rub(total_balance)}\n\n"
            "📌 Обеспечение по листингам\n"
            f"Заблокировано: {self._format_usdt_with_rub(locked_total)}\n"
            f"Требуется: {self._format_usdt_with_rub(required_total)}"
        )
        await self._replace_message(query_message, text, self._seller_balance_menu_markup())

    async def _render_seller_topup_history(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        intents = await self._deposit_service.list_seller_deposit_intents(
            seller_user_id=seller_user_id,
            limit=10,
        )
        if not intents:
            await self._replace_message(
                query_message,
                "🧾 Транзакций пока нет. Нажмите «➕ Пополнить».",
                self._seller_balance_menu_markup(),
            )
            return

        lines = ["🧾 Транзакции:"]
        for item in intents:
            expected_amount = self._format_usdt_value(item.expected_amount_usdt, precise=True)
            block = (
                f"• Сумма: {expected_amount} USDT\n"
                f"Статус: {self._humanize_deposit_status(item.status)}\n"
                f"Создан: {item.created_at:%d.%m.%Y %H:%M UTC}\n"
                f"Срок счета: до {item.expires_at:%d.%m.%Y %H:%M UTC}"
            )
            if item.status == "credited" and item.credited_amount_usdt is not None:
                block += (
                    f"\nЗачислено: "
                    f"{self._format_usdt_value(item.credited_amount_usdt, precise=True)} USDT"
                )
            if item.status == "manual_review":
                block += "\nПеревод найден, но нужна проверка администратором."
            if item.status == "expired":
                block += "\nЕсли вы оплатили после срока, обратитесь к администратору."
            lines.append(block)

        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            self._seller_balance_menu_markup(),
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
            last_slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            await self._render_buyer_shops_section(
                query_message=query_message,
                last_shop_slug=last_slug or None,
            )
            return
        if action == "open_last_shop":
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            if not slug:
                await self._replace_message(
                    query_message,
                    "Нет сохраненного магазина. Нажмите «🔎 Открыть магазин по коду».",
                    self._buyer_menu_markup(),
                )
                return
            context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = slug
            await self._send_buyer_shop_catalog(query_message, slug=slug)
            await query_message.reply_text(
                "Выберите действие:",
                reply_markup=self._buyer_menu_markup(),
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
                self._buyer_menu_markup(),
            )
            return
        if action == "reserve":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть выбранный товар. Попробуйте снова.",
                    self._buyer_menu_markup(),
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
                    self._buyer_menu_markup(),
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
                (
                    "Отправьте код подтверждения покупки (base64) следующим сообщением."
                ),
                self._buyer_menu_markup(),
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
                self._buyer_menu_markup(),
            )
            return
        if action == "withdraw_history":
            await self._render_buyer_withdraw_history(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
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

        text = (
            f"<b>Задания:</b> {in_progress} в процессе · {ready} к выводу · "
            f"{paid} выплачено · {len(assignments)} всего\n"
            f"<b>Баланс:</b> {self._format_usdt_with_rub(total_balance)} · "
            f"{self._format_usdt_with_rub(snapshot.buyer_available_usdt)} доступно"
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
        last_shop_slug: str | None,
    ) -> None:
        lines = ["🏪 Раздел магазинов"]
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
        if last_shop_slug:
            lines.append(f"Последний код магазина: {last_shop_slug}")
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
            lines.append("Последний магазин не сохранен.")

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="🧭 Дашборд покупателя",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            "\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
            await self._replace_message(query_message, "Товар больше недоступен.")
            return
        except NoSlotsAvailableError:
            await self._replace_message(
                query_message,
                "Свободных мест нет. Попробуйте выбрать другой товар.",
            )
            return
        except InvalidStateError:
            await self._replace_message(
                query_message,
                "Не удалось забронировать место. Попробуйте снова.",
            )
            return

        if reservation.created:
            text = (
                "Место забронировано.\n"
                "Отправьте код подтверждения покупки в течение 2 часов.\n"
                f"Срок: до {reservation.reservation_expires_at:%d.%m.%Y %H:%M UTC}"
            )
        else:
            text = (
                "У вас уже есть активная бронь по этому товару.\n"
                "Срок отправки подтверждения: "
                f"до {reservation.reservation_expires_at:%d.%m.%Y %H:%M UTC}"
            )
        self._logger.info(
            "buyer_slot_reserved",
            listing_id=listing_id,
            assignment_id=reservation.assignment_id,
            created=reservation.created,
        )
        await self._replace_message(query_message, text, self._buyer_menu_markup())

    async def _render_buyer_assignments(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        assignments = await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        if not assignments:
            await self._replace_message(
                query_message,
                "📋 У вас пока нет заданий.",
                self._buyer_menu_markup(),
            )
            return

        lines = ["📋 Мои задания:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for idx, item in enumerate(assignments, start=1):
            lines.append(
                f"• Задание {idx} · магазин: {item.shop_slug}\n"
                f"Статус: {self._humanize_assignment_status(item.status)}\n"
                f"Кэшбэк: {self._format_usdt_value(item.reward_usdt, precise=True)} USDT"
            )
            if item.order_id:
                lines.append(f"Номер заказа: {item.order_id}")
            if item.status in {"reserved", "order_submitted"}:
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text="📤 Отправить подтверждение покупки",
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
                    text="🧭 Дашборд покупателя",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
        text = (
            "💳 Баланс покупателя\n"
            f"Доступно: {self._format_usdt_with_rub(snapshot.buyer_available_usdt)}\n"
            f"В ожидании вывода: {self._format_usdt_with_rub(snapshot.buyer_withdraw_pending_usdt)}"
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
                            text="🧾 История выводов",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="withdraw_history",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🧭 Дашборд покупателя",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                        )
                    ],
                ]
            ),
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
                self._buyer_menu_markup(),
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
            self._buyer_menu_markup(),
        )

    async def _render_buyer_withdraw_history(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        history = await self._finance_service.list_buyer_withdrawal_history(
            buyer_user_id=buyer_user_id
        )
        if not history:
            await self._replace_message(
                query_message,
                "🧾 История выводов пока пустая.",
                self._buyer_menu_markup(),
            )
            return

        lines = ["🧾 Транзакции вывода:"]
        for item in history:
            block = (
                f"• Сумма: {self._format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
                f"Статус: {self._humanize_withdraw_status(item.status)}\n"
                f"Адрес: {item.payout_address}"
            )
            if item.tx_hash:
                block += f"\nХэш перевода: {item.tx_hash}"
            lines.append(block)
        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            self._buyer_menu_markup(),
        )

    async def _ensure_admin_user(self, *, telegram_id: int, username: str | None) -> int:
        async with self._db_pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT id, role
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
                            INSERT INTO users (telegram_id, username, role)
                            VALUES (%s, %s, 'admin')
                            RETURNING id
                            """,
                            (telegram_id, username),
                        )
                        created = await cur.fetchone()
                        return created["id"]
                    if existing["role"] != "admin":
                        raise InvalidStateError("telegram user exists with non-admin role")
                    if username is not None:
                        await cur.execute(
                            """
                            UPDATE users
                            SET username = %s,
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

        text = (
            f"<b>Выводы в очереди:</b> {len(pending_withdrawals)}\n"
            f"<b>Платежи на ручной разбор:</b> {len(review_txs)}\n"
            f"<b>Просроченные счета:</b> {len(expired_intents)}"
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
            "💸 Раздел выводов\nВыберите действие.",
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
                            text="🧭 Дашборд админа",
                            callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                        )
                    ],
                ]
            ),
        )

    async def _render_admin_deposits_section(self, *, query_message: Message | None) -> None:
        await self._replace_message(
            query_message,
            "🏦 Раздел пополнений\nВыберите действие.",
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
                            text="🧭 Дашборд админа",
                            callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                        )
                    ],
                ]
            ),
        )

    async def _render_admin_pending_withdrawals(self, *, query_message: Message | None) -> None:
        pending = await self._finance_service.list_pending_withdrawals()
        if not pending:
            await self._replace_message(
                query_message,
                "💸 Очередь выводов пуста.",
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
                                text="🧭 Дашборд админа",
                                callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                            )
                        ],
                    ]
                ),
            )
            return

        lines = ["💸 Очередь заявок на вывод:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for item in pending:
            lines.append(
                f"Заявка #{item.withdrawal_request_id}\n"
                f"Покупатель: {item.buyer_telegram_id} (@{item.buyer_username or '-'})\n"
                f"Сумма: {self._format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
                f"Кошелек: {item.payout_address}"
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
                    text="🧭 Дашборд админа",
                    callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            "\n\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
            f"📄 Заявка #{detail.withdrawal_request_id}",
            f"Покупатель: {detail.buyer_telegram_id} (@{detail.buyer_username or '-'})",
            f"Сумма: {self._format_usdt_value(detail.amount_usdt, precise=True)} USDT",
            f"Статус: {self._humanize_withdraw_status(detail.status)}",
            f"Кошелек: {detail.payout_address}",
            f"Создана: {detail.requested_at:%d.%m.%Y %H:%M UTC}",
            (
                f"Обработана: {detail.processed_at:%d.%m.%Y %H:%M UTC}"
                if detail.processed_at
                else "Обработана: -"
            ),
            (
                f"Отправлена: {detail.sent_at:%d.%m.%Y %H:%M UTC}"
                if detail.sent_at
                else "Отправлена: -"
            ),
        ]
        if detail.tx_hash:
            lines.append(f"Хэш перевода: {detail.tx_hash}")
        if detail.note:
            lines.append(f"Комментарий: {detail.note}")
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
            "\n".join(lines),
            InlineKeyboardMarkup(keyboard_rows),
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
            created=result.created,
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
                    f"Истек: {intent.expires_at:%d.%m.%Y %H:%M UTC}"
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
                        text="🧭 Дашборд админа",
                        callback_data=build_callback(flow=_ROLE_ADMIN, action="menu"),
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
                        SELECT id, role
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
                    if user_row["role"] != required_role:
                        raise InvalidStateError(
                            f"user role '{user_row['role']}' is incompatible with {account_kind}"
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
                self._admin_menu_markup(),
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
                self._admin_menu_markup(),
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
                self._admin_menu_markup(),
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
                self._admin_menu_markup(),
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
                self._admin_menu_markup(),
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
                self._admin_menu_markup(),
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
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
                )
                return
            if self._wb_ping_client is None:
                self._clear_prompt(context)
                await message.reply_text("Проверка токена временно недоступна. Попробуйте позже.")
                return

            ping_result = await self._wb_ping_client.validate_token(wb_token)
            if not ping_result.valid:
                details = ping_result.message or "неизвестная ошибка"
                await message.reply_text(
                    (
                        "Токен не прошел проверку и не сохранен.\n"
                        f"Причина: {details}\n"
                        "Проверьте, что токен «Базовый», с правом «Статистика: только чтение», "
                        "и отправьте его снова."
                    ),
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
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
                    "Шаг 2/2: введите название магазина следующим сообщением."
                ),
                reply_markup=self._seller_shops_menu_markup(has_shops=True),
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
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
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
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
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
                    reply_markup=self._seller_shops_menu_markup(has_shops=True),
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
            response = await self._seller_processor.handle(
                telegram_id=identity.telegram_id,
                username=identity.username,
                text=f"/token_set {shop_id} {text}",
            )
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
            tokens = text.split()
            if len(tokens) != 4:
                await message.reply_text(
                    (
                        "Введите данные одной строкой:\n"
                        "<артикул_WB> <скидка_%> <кэшбэк_USDT> <мест>\n"
                        "Пример: 12345678 20 1.5 10"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к листингам",
                                    callback_data=build_callback(
                                        flow=_ROLE_SELLER,
                                        action="listings",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            try:
                wb_product_id = int(tokens[0])
                discount_percent = int(tokens[1])
                reward_usdt = Decimal(tokens[2])
                slots = int(tokens[3])
                listing = await self._seller_service.create_listing_draft(
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    wb_product_id=wb_product_id,
                    discount_percent=discount_percent,
                    reward_usdt=reward_usdt,
                    slot_count=slots,
                )
            except (ValueError, InvalidOperation):
                await message.reply_text(
                    (
                        "Не удалось разобрать данные.\n"
                        "Проверьте формат и отправьте строку еще раз."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к листингам",
                                    callback_data=build_callback(
                                        flow=_ROLE_SELLER,
                                        action="listings",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            except (NotFoundError, InvalidStateError, InsufficientFundsError):
                await message.reply_text(
                    (
                        "Не удалось создать листинг.\n"
                        "Проверьте токен магазина, баланс и введенные значения."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к листингам",
                                    callback_data=build_callback(
                                        flow=_ROLE_SELLER,
                                        action="listings",
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return

            self._clear_prompt(context)
            await message.reply_text(
                (
                    "Листинг создан.\n"
                    f"Кэшбэк: {self._format_usdt_value(listing.reward_usdt, precise=True)} USDT\n"
                    f"Слоты: {listing.available_slots}/{listing.slot_count}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="📦 К листингам",
                                callback_data=build_callback(
                                    flow=_ROLE_SELLER,
                                    action="listings",
                                ),
                            )
                        ]
                    ]
                ),
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
            await message.reply_text(
                (
                    "Счет на пополнение создан.\n"
                    f"Срок действия: {self._settings.seller_collateral_invoice_ttl_hours} ч\n"
                    "Сеть: USDT в сети TON (не ERC-20)\n"
                    f"Адрес: {intent.deposit_address}\n"
                    "Сумма (должна полностью совпадать): "
                    f"{self._format_usdt_value(intent.expected_amount_usdt, precise=True)} USDT\n\n"
                    "После перевода нажмите «🧾 Транзакции»."
                ),
                reply_markup=self._seller_balance_menu_markup(),
            )
            return

        if prompt_type == "buyer_shop_slug":
            self._clear_prompt(context)
            context.user_data[_LAST_BUYER_SHOP_SLUG_KEY] = text
            await self._send_buyer_shop_catalog(message, slug=text)
            await message.reply_text(
                "Выберите действие:",
                reply_markup=self._buyer_menu_markup(),
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
                details = str(exc).strip()
                base = (
                    "Код подтверждения не принят.\n"
                    "Проверьте, что вы отправили полный код из расширения для этого задания."
                )
                if details:
                    await message.reply_text(f"{base}\nПричина: {details}")
                else:
                    await message.reply_text(base)
                return
            except DuplicateOrderError:
                await message.reply_text("Этот номер заказа уже использован в другом задании.")
                return
            except InvalidStateError:
                await message.reply_text("Сейчас нельзя отправить подтверждение для этого задания.")
                return

            self._clear_prompt(context)
            if result.changed:
                reply = (
                    "Подтверждение принято.\n"
                    f"Номер заказа: {result.order_id}\n"
                    "Дальше мы автоматически проверим выкуп и начисление кэшбэка."
                )
            else:
                reply = (
                    "Это подтверждение уже было отправлено ранее.\n"
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
                reply_markup=self._buyer_menu_markup(),
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

    async def _send_buyer_shop_catalog(self, message: Message, *, slug: str) -> None:
        try:
            shop = await self._buyer_service.resolve_shop_by_slug(slug=slug)
            listings = await self._buyer_service.list_active_listings_by_shop_slug(slug=slug)
        except (NotFoundError, InvalidStateError):
            await message.reply_text("Магазин недоступен. Проверьте ссылку и попробуйте снова.")
            return

        header = f"Магазин: {shop.title}"
        if not listings:
            await message.reply_text(f"🏪 {header}\n📦 Активных листингов пока нет.")
            return

        lines = [f"🏪 {header}", "📦 Активные листинги:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for idx, listing in enumerate(listings, start=1):
            lines.append(
                f"• Товар {idx}\n"
                f"Артикул WB: {listing.wb_product_id}\n"
                f"Скидка: {listing.discount_percent}%\n"
                f"Кэшбэк: {self._format_usdt_value(listing.reward_usdt, precise=True)} USDT\n"
                f"Свободно мест: {listing.available_slots} из {listing.slot_count}"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Забронировать место",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="reserve",
                            entity_id=str(listing.listing_id),
                        ),
                    )
                ]
            )
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows))

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
    def _format_decimal(amount: Decimal, *, quant: Decimal) -> str:
        normalized = amount.quantize(quant, rounding=ROUND_HALF_UP)
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def _format_usdt(self, amount: Decimal, *, precise: bool = False) -> str:
        quant = _USDT_EXACT_QUANT if precise else _USDT_SUMMARY_QUANT
        return f"${self._format_decimal(amount, quant=quant)}"

    def _format_usdt_value(self, amount: Decimal, *, precise: bool = False) -> str:
        quant = _USDT_EXACT_QUANT if precise else _USDT_SUMMARY_QUANT
        return self._format_decimal(amount, quant=quant)

    def _format_rub_approx(self, amount: Decimal) -> str:
        rub = amount * self._display_rub_per_usdt
        return f"~{self._format_decimal(rub, quant=_RUB_QUANT)} ₽"

    def _format_usdt_with_rub(self, amount: Decimal, *, precise: bool = False) -> str:
        return f"{self._format_usdt(amount, precise=precise)} ({self._format_rub_approx(amount)})"

    @staticmethod
    def _humanize_listing_status(status: str) -> str:
        mapping = {
            "draft": "Черновик",
            "active": "Активен",
            "paused": "На паузе",
        }
        return mapping.get(status, status)

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
            await message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
        except Exception:
            await message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)

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
                        text="🏬 Магазины",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                    ),
                    InlineKeyboardButton(
                        text="📦 Листинги",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💰 Баланс",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="balance"),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Сменить роль",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="back"),
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
                        text="🧭 Дашборд продавца",
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
                [
                    InlineKeyboardButton(
                        text="🔄 Сменить роль",
                        callback_data=build_callback(flow=_ROLE_BUYER, action="back"),
                    )
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
                [
                    InlineKeyboardButton(
                        text="🔄 Сменить роль",
                        callback_data=build_callback(flow=_ROLE_ADMIN, action="back"),
                    )
                ],
            ]
        )

    async def _handle_error(
        self,
        update: object,
        context: CallbackContext,
    ) -> None:
        error = context.error
        update_id = update.update_id if isinstance(update, Update) else None
        if isinstance(error, DomainError):
            self._logger.warning(
                "telegram_domain_error",
                update_id=update_id,
                error_type=type(error).__name__,
                error_message=str(error)[:500],
            )
            return
        self._logger.exception(
            "telegram_update_handler_failed",
            update_id=update_id,
            error_type=type(error).__name__ if error else None,
            error_message=str(error)[:500] if error else None,
        )

    def _health_payload(self) -> dict[str, Any]:
        return {
            "service": "bot_api",
            "ready": self._ready,
            "status": "ok" if self._ready else "starting",
        }

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
