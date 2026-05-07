from __future__ import annotations

import asyncio
import base64
import html
import json
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
from libs.domain.listing_creation import parse_listing_create_csv, sanitize_buyer_display_title
from libs.domain.notifications import NotificationService
from libs.domain.public_refs import (
    build_support_deep_link,
    format_assignment_ref,
    format_chain_tx_ref,
    format_deposit_ref,
    format_listing_ref,
    format_shop_ref,
    format_withdrawal_ref,
    parse_assignment_ref,
    parse_chain_tx_ref,
    parse_deposit_ref,
    parse_withdrawal_ref,
)
from libs.domain.seller import SellerService
from libs.domain.seller_workflow import SellerWorkflowService
from libs.integrations.fx_rates import CoinGeckoUsdtRubClient
from libs.integrations.tonapi import TonapiApiError, TonapiClient
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
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.telegram_notifications import render_telegram_notification
from services.bot_api.transport_effects import (
    AnswerCallback,
    ButtonSpec,
    ClearPrompt,
    DeleteSourceMessage,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyRoleMenuText,
    ReplyText,
    SetPrompt,
)
from services.bot_api.withdrawal_flow import (
    BUYER_WITHDRAWAL_CONFIG,
    SELLER_WITHDRAWAL_CONFIG,
    AddressValidationUnavailable,
    TonMainnetAddressValidator,
    WithdrawalRequestCreationFlow,
    WithdrawalRequester,
    WithdrawalRequesterAdapter,
)

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
_BUYER_TASK_COMPANION_PRODUCTS = 1
_QPILKA_EXTENSION_URL = "https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga"
_NUMBERED_PAGE_SIZE = 10
_MSK_TZ = ZoneInfo("Europe/Moscow")
_TON_FRIENDLY_MAINNET_PREFIXES = frozenset({"E", "U"})
_TON_FRIENDLY_TESTNET_PREFIXES = frozenset({"k", "0"})

_SELLER_COMMAND_PREFIXES = (
    "/shop_",
    "/token_set",
    "/listing_",
)
_BUYER_COMMAND_PREFIXES = (
    "/shop",
    "/reserve",
    "/submit_order",
    "/submit_review",
    "/my_orders",
)
_RUNTIME_REQUIRED_SCHEMA_COLUMNS = {
    "users": {
        "is_seller",
        "is_buyer",
        "is_admin",
    },
    "assignments": {
        "task_uuid",
        "wb_product_id",
        "review_required",
        "review_phrases",
    },
    "buyer_orders": {
        "task_uuid",
        "wb_product_id",
    },
    "buyer_reviews": {
        "assignment_id",
        "task_uuid",
        "wb_product_id",
        "reviewed_at",
        "rating",
        "review_text",
        "verification_status",
        "verification_reason",
        "verified_at",
        "verified_by_admin_user_id",
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
        "review_phrases",
        "reference_price_rub",
        "reference_price_source",
        "reference_price_updated_at",
    },
    "notification_outbox": {
        "recipient_telegram_id",
        "event_type",
        "dedupe_key",
        "status",
        "next_attempt_at",
    },
}
_NOTIFICATION_DISPATCH_POLL_SECONDS = 2.0
_NOTIFICATION_DISPATCH_BATCH_SIZE = 50
_NOTIFICATION_DISPATCH_MAX_BACKOFF_SECONDS = 3600


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None


class _RuntimeSellerListingWorkflowAdapter:
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def load_listing_creation_snapshot(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbProductSnapshot:
        return await self._runtime._load_listing_creation_snapshot(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            wb_product_id=wb_product_id,
        )

    async def lookup_listing_buyer_price(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
    ) -> WbObservedBuyerPrice | None:
        return await self._runtime._lookup_listing_buyer_price(
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            wb_product_id=wb_product_id,
        )

    @staticmethod
    def reference_price_updated_at(
        *,
        observed_buyer_price: WbObservedBuyerPrice | None,
        reference_price_source: str,
    ) -> datetime:
        if reference_price_source == "orders" and observed_buyer_price is not None:
            return observed_buyer_price.observed_at or datetime.now(UTC)
        return datetime.now(UTC)


class _RuntimeTonMainnetAddressValidator(TonMainnetAddressValidator):
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def validate(self, *, address: str) -> None:
        try:
            await self._runtime._parse_ton_mainnet_address(address=address)
        except TonapiApiError as exc:
            raise AddressValidationUnavailable from exc


class _RuntimeSellerWithdrawalAdapter(WithdrawalRequesterAdapter):
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def get_active_request(self, *, requester_user_id: int) -> Any | None:
        return await self._runtime._finance_service.get_active_seller_withdrawal_request(
            seller_user_id=requester_user_id
        )

    async def get_available_balance(self, *, requester_user_id: int) -> Decimal:
        snapshot = await self._runtime._seller_service.get_seller_balance_snapshot(seller_user_id=requester_user_id)
        return snapshot.seller_available_usdt

    async def load_requester(self, *, telegram_id: int, username: str | None) -> WithdrawalRequester:
        seller = await self._runtime._seller_service.bootstrap_seller(
            telegram_id=telegram_id,
            username=username,
        )
        return WithdrawalRequester(
            user_id=seller.user_id,
            available_account_id=seller.seller_available_account_id,
            pending_account_id=seller.seller_withdraw_pending_account_id,
        )

    async def create_withdrawal_request(
        self,
        *,
        requester: WithdrawalRequester,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._finance_service.create_withdrawal_request(
            requester_user_id=requester.user_id,
            requester_role="seller",
            from_account_id=requester.available_account_id,
            pending_account_id=requester.pending_account_id,
            amount_usdt=amount_usdt,
            payout_address=payout_address,
            idempotency_key=idempotency_key,
        )

    async def get_withdrawal_request_detail(self, *, request_id: int) -> Any:
        return await self._runtime._finance_service.get_withdrawal_request_detail(request_id=request_id)

    async def cancel_withdrawal_request(
        self,
        *,
        request_id: int,
        requester_user_id: int,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._finance_service.cancel_withdrawal_request(
            request_id=request_id,
            requester_user_id=requester_user_id,
            requester_role="seller",
            idempotency_key=idempotency_key,
        )


class _RuntimeBuyerWithdrawalAdapter(WithdrawalRequesterAdapter):
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def get_active_request(self, *, requester_user_id: int) -> Any | None:
        return await self._runtime._finance_service.get_active_buyer_withdrawal_request(buyer_user_id=requester_user_id)

    async def get_available_balance(self, *, requester_user_id: int) -> Decimal:
        snapshot = await self._runtime._finance_service.get_buyer_balance_snapshot(buyer_user_id=requester_user_id)
        return snapshot.buyer_available_usdt

    async def load_requester(self, *, telegram_id: int, username: str | None) -> WithdrawalRequester:
        buyer = await self._runtime._buyer_service.bootstrap_buyer(
            telegram_id=telegram_id,
            username=username,
        )
        return WithdrawalRequester(
            user_id=buyer.user_id,
            available_account_id=buyer.buyer_available_account_id,
            pending_account_id=buyer.buyer_withdraw_pending_account_id,
        )

    async def create_withdrawal_request(
        self,
        *,
        requester: WithdrawalRequester,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._finance_service.create_withdrawal_request(
            requester_user_id=requester.user_id,
            requester_role="buyer",
            from_account_id=requester.available_account_id,
            pending_account_id=requester.pending_account_id,
            amount_usdt=amount_usdt,
            payout_address=payout_address,
            idempotency_key=idempotency_key,
        )

    async def get_withdrawal_request_detail(self, *, request_id: int) -> Any:
        return await self._runtime._finance_service.get_withdrawal_request_detail(request_id=request_id)

    async def cancel_withdrawal_request(
        self,
        *,
        request_id: int,
        requester_user_id: int,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._finance_service.cancel_withdrawal_request(
            request_id=request_id,
            requester_user_id=requester_user_id,
            requester_role="buyer",
            idempotency_key=idempotency_key,
        )


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
        self._seller_workflow_service: SellerWorkflowService | None = None
        self._buyer_service: BuyerService | None = None
        self._finance_service: FinanceService | None = None
        self._deposit_service: DepositIntentService | None = None
        self._notification_service: NotificationService | None = None
        self._fx_rate_service: FxRateService | None = None
        self._seller_processor: SellerCommandProcessor | None = None
        self._buyer_processor: BuyerCommandProcessor | None = None
        self._seller_listing_creation_flow: SellerListingCreationFlow | None = None
        self._wb_ping_client: WbPingClient | None = None
        self._wb_public_client: WbPublicCatalogClient | None = None
        self._tonapi_client: TonapiClient | None = None
        self._payout_wallet_raw_form: str | None = None
        self._display_rub_per_usdt = settings.display_rub_per_usdt
        self._notification_dispatch_task: asyncio.Task[None] | None = None

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
            self._notification_service = NotificationService(self._db_pool.pool)
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
            self._tonapi_client = TonapiClient(
                base_url=self._settings.tonapi_base_url,
                api_key=self._settings.tonapi_api_key,
                timeout_seconds=self._settings.tonapi_timeout_seconds,
                unauth_min_interval_seconds=self._settings.tonapi_unauth_min_interval_seconds,
            )
            self._payout_wallet_raw_form = None
            seller_workflow_service = SellerWorkflowService(
                seller_service=self._seller_service,
                wb_public_client=self._wb_public_client,
                token_cipher_key=self._settings.token_cipher_key,
            )
            self._seller_processor = SellerCommandProcessor(
                seller_service=self._seller_service,
                seller_workflow_service=seller_workflow_service,
                wb_ping_client=wb_ping_client,
                token_cipher_key=self._settings.token_cipher_key,
                bot_username=self._settings.telegram_bot_username,
                display_rub_per_usdt=self._settings.display_rub_per_usdt,
                fx_rate_service=self._fx_rate_service,
                fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
            )
            self._seller_workflow_service = seller_workflow_service
            self._seller_listing_creation_flow = SellerListingCreationFlow(
                seller_service=self._seller_service,
                seller_workflow=seller_workflow_service,
                display_rub_per_usdt=self._settings.display_rub_per_usdt,
                fx_rate_service=self._fx_rate_service,
                fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
            )
            self._buyer_processor = BuyerCommandProcessor(
                buyer_service=self._buyer_service,
                bot_username=self._settings.telegram_bot_username,
                display_rub_per_usdt=self._settings.display_rub_per_usdt,
            )
            await self._notification_service.sync_admin_users(
                telegram_ids=self._settings.admin_telegram_ids,
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
            self._notification_dispatch_task = asyncio.create_task(
                self._notification_dispatch_loop(bot=application.bot)
            )
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
        if self._notification_dispatch_task is not None:
            self._notification_dispatch_task.cancel()
            try:
                await self._notification_dispatch_task
            except asyncio.CancelledError:
                pass
            self._notification_dispatch_task = None
        await self._db_pool.close()
        self._logger.info("telegram_webhook_runtime_stopped")

    async def _ensure_webhook_registration(self, *, application: Application) -> None:
        desired_url = self._build_webhook_url()
        cert_path = self._settings.webhook_tls_cert_path
        key_path = self._settings.webhook_tls_key_path
        has_custom_certificate = bool(cert_path and key_path)
        webhook_info = await application.bot.get_webhook_info()
        webhook_matches = (
            webhook_info.url == desired_url and webhook_info.has_custom_certificate is has_custom_certificate
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
            await update.message.reply_text("Команда не распознана. Используйте /start и кнопки меню.")
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
        if action == "kb_guide":
            await self._render_seller_knowledge_screen(query_message=query_message, topic="guide")
            return
        if action == "kb_shops":
            await self._render_seller_knowledge_screen(query_message=query_message, topic="shops")
            return
        if action == "kb_listings":
            await self._render_seller_knowledge_screen(
                query_message=query_message,
                topic="listings",
            )
            return
        if action == "kb_balance":
            await self._render_seller_knowledge_screen(query_message=query_message, topic="balance")
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
                self._screen_text(
                    title=f"Переименование магазина «{html.escape(shop.title)}»",
                    cta="Введите новое название магазина следующим сообщением ниже.",
                    lines=[
                        "При переименовании ссылка магазина изменится, старая перестанет работать.",
                        "Название видят покупатели, поэтому используйте нейтральное и понятное имя.",
                    ],
                    warning=True,
                ),
                self._seller_shop_detail_markup(
                    shop_id=shop_id,
                    token_is_valid=self._is_valid_shop_token(shop.wb_token_status),
                ),
                parse_mode="HTML",
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
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=self._get_seller_listing_creation_flow().start_prompt(
                    seller_user_id=seller.user_id,
                    shop_id=shop_id,
                    shop_title=shop.title,
                ),
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
            if payload.entity_id:
                await self._render_seller_listing_detail(
                    query_message=query_message,
                    seller_user_id=seller.user_id,
                    listing_id=int(payload.entity_id),
                    list_page=self._seller_listings_page_from_context(context),
                )
                return
            await self._replace_message(
                query_message,
                "Не удалось открыть карточку объявления. Попробуйте еще раз.",
                self._seller_menu_markup(),
            )
            return
        if action == "listing_title_keep":
            prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
            if not isinstance(prompt_state, dict):
                await self._replace_message(
                    query_message,
                    "Не удалось продолжить создание объявления. Откройте раздел заново.",
                    self._seller_back_markup(action="listings", label="↩️ К объявлениям"),
                )
                return
            result = await self._get_seller_listing_creation_flow().create_draft_from_prompt(
                prompt_state=prompt_state,
            )
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=result,
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
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=self._get_seller_listing_creation_flow().title_edit_prompt(
                    prompt_state=prompt_state,
                ),
            )
            return
        if action == "listing_edit":
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Редактирование отключено",
                    lines=[
                        "Редактирование объявлений недоступно, чтобы не создавать конфликтов с уже начатыми покупками.",
                    ],
                    note=("Если нужно изменить параметры, создайте новое объявление и удалите старое."),
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
        if action == "withdraw_full":
            await self._start_seller_withdraw_full_amount(
                context=context,
                query_message=query_message,
                seller_user_id=seller.user_id,
            )
            return
        if action == "withdraw_prompt_amount":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=await self._seller_withdrawal_creation_flow().start_manual_amount_prompt(
                    requester_user_id=seller.user_id
                ),
            )
            return
        if action == "withdraw_cancel_prompt":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=await self._seller_withdrawal_creation_flow().start_cancel_prompt(
                    requester_user_id=seller.user_id,
                    request_id=int(payload.entity_id) if payload.entity_id else None,
                ),
            )
            return
        if action == "withdraw_cancel_confirm":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=await self._seller_withdrawal_creation_flow().confirm_cancel(
                    requester_user_id=seller.user_id,
                    request_id=int(payload.entity_id) if payload.entity_id else None,
                ),
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
                InlineKeyboardMarkup(
                    [
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
            )
            return
        if action == "topup_history":
            await self._render_seller_transaction_history(
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
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        balance = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        orders = await self._load_seller_order_counters(seller_user_id=seller_user_id)

        listings_active = sum(1 for item in listings if item.status == "active")
        listings_total = len(listings)
        shops_total = len(shops)
        shops_active = sum(1 for item in shops if self._is_valid_shop_token(item.wb_token_status))
        balance_free = balance.seller_available_usdt
        balance_total = (
            balance.seller_available_usdt + balance.seller_collateral_usdt + balance.seller_withdraw_pending_usdt
        )

        text = self._screen_text(
            title="Кабинет продавца",
            cta="Выберите раздел ниже.",
            lines=[
                f"<b>Магазины:</b> {shops_total} · {shops_active} активно",
                f"<b>Объявления:</b> {listings_total} · {listings_active} активно",
                "<b>Покупки:</b> "
                f"ожидают заказа: {orders['awaiting_order']} · "
                f"заказаны: {orders['ordered']} · "
                f"выкуплены: {orders['picked_up']}",
                f"<b>Баланс:</b> {self._format_usdt_with_rub(balance_total)}",
                f"<b>Свободно:</b> {self._format_usdt_with_rub(balance_free)}",
            ],
            note="Откройте нужный раздел ниже.",
        )
        await self._replace_message(
            query_message,
            text,
            self._seller_menu_markup(
                listings_count=listings_total,
                shops_count=shops_total,
            ),
            parse_mode="HTML",
        )

    async def _render_seller_knowledge_screen(
        self,
        *,
        query_message: Message | None,
        topic: str,
    ) -> None:
        if topic == "guide":
            text = self._screen_text(
                title="Инструкция продавца",
                cta=(
                    "Купилка помогает выдать кэшбэк за покупку и отзыв, "
                    "а обеспечение держит на балансе до завершения покупки."
                ),
                lines=[
                    (
                        "<b>Как запустить витрину</b>\n"
                        "1. Создайте магазин и добавьте WB API токен в режиме чтения.\n"
                        "2. Создайте объявление: артикул WB, кэшбэк в ₽, число покупок, поисковая фраза "
                        "и до 10 фраз для отзыва.\n"
                        "3. Пополните баланс, если не хватает обеспечения.\n"
                        "4. Активируйте объявление и отправьте покупателям ссылку на магазин.\n"
                        "5. Следите за покупками и выводите свободный остаток."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Покупатели видят название магазина и название объявления.\n"
                        "2. Объявление нельзя менять после создания: если условия изменились, создайте новое "
                        "и удалите старое.\n"
                        "3. Деньги под активные объявления нельзя вывести, пока они зарезервированы."
                    ),
                ],
                note="Подробности по каждому разделу открываются кнопками ниже.",
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_SELLER, topic="shops"),
                        self._knowledge_button(role=_ROLE_SELLER, topic="listings"),
                    ],
                    [self._knowledge_button(role=_ROLE_SELLER, topic="balance")],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                        )
                    ],
                ]
            )
        elif topic == "shops":
            text = self._screen_text(
                title="Про магазины",
                cta="Магазин — это ваша публичная витрина с отдельной ссылкой для покупателей.",
                lines=[
                    (
                        "В магазине находятся объявления. Покупатель переходит по ссылке магазина, "
                        "выбирает товар и начинает покупку."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Название магазина видят покупатели, поэтому используйте нейтральное и понятное имя.\n"
                        "2. При переименовании ссылка меняется, старая перестает работать.\n"
                        "3. Без валидного WB API токена объявления нельзя активировать."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_SELLER, topic="guide"),
                        self._knowledge_button(role=_ROLE_SELLER, topic="listings"),
                    ],
                    [self._knowledge_button(role=_ROLE_SELLER, topic="balance")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К магазинам",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                        )
                    ],
                ]
            )
        elif topic == "listings":
            text = self._screen_text(
                title="Про объявления",
                cta="Объявление описывает один товар WB, размер кэшбэка и число доступных покупок.",
                lines=[
                    (
                        "После создания бот фиксирует кэшбэк в USDT, проверяет карточку WB "
                        "и показывает покупателю только нужные поля. После выкупа покупатель "
                        "подтверждает отзыв на 5 звезд."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Если параметр нужно изменить, создайте новое объявление.\n"
                        "2. Активация требует валидный токен WB, достаточное обеспечение и живую карточку товара.\n"
                        "3. Для отзыва можно указать до 10 фраз; покупатель получит до двух случайных фраз.\n"
                        "4. Если средств не хватает, сначала пополните баланс продавца."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_SELLER, topic="guide"),
                        self._knowledge_button(role=_ROLE_SELLER, topic="shops"),
                    ],
                    [self._knowledge_button(role=_ROLE_SELLER, topic="balance")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К объявлениям",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                        )
                    ],
                ]
            )
        else:
            text = self._screen_text(
                title="Про баланс и вывод",
                cta="Баланс продавца делится на свободные средства, обеспечение объявлений и вывод.",
                lines=[
                    (
                        "Свободный остаток можно использовать для новых объявлений или вывести. "
                        "Обеспечение активных объявлений и сумма в заявке на вывод временно недоступны."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Пополнение и вывод работают в USDT в сети TON.\n"
                        "2. Если есть активная заявка на вывод, новую создать нельзя.\n"
                        "3. История показывает и пополнения, и выводы в одном разделе."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_SELLER, topic="guide"),
                        self._knowledge_button(role=_ROLE_SELLER, topic="shops"),
                    ],
                    [self._knowledge_button(role=_ROLE_SELLER, topic="listings")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К балансу",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="balance"),
                        )
                    ],
                ]
            )
        await self._replace_message(query_message, text, markup, parse_mode="HTML")

    async def _load_seller_order_counters(self, *, seller_user_id: int) -> dict[str, int]:
        if self._seller_service is None:
            return {"awaiting_order": 0, "ordered": 0, "picked_up": 0}
        return await self._seller_service.get_seller_order_counters(seller_user_id=seller_user_id)

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
                    text=f"🏬 {shop.title} · {self._shop_ref(shop.shop_id)}",
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
                title_suffix_html=self._title_ref_suffix(self._shop_ref(shop.shop_id)),
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
                [self._knowledge_button(role=_ROLE_SELLER, topic="shops")],
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
        keyboard_rows: list[list[InlineKeyboardButton]] = [
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
        ]
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ К списку магазинов",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                )
            ]
        )
        keyboard_rows.append([self._knowledge_button(role=_ROLE_SELLER, topic="shops")])
        return InlineKeyboardMarkup(keyboard_rows)

    def _shop_token_instruction_text(self, *, shop_title: str | None = None) -> str:
        title = f"Токен WB API для магазина «{html.escape(shop_title)}»" if shop_title else "Создание магазина"
        lines = [
            "<b>Шаг 1 из 2.</b>",
            (
                "<b>Как создать:</b> Создайте Базовый токен в режиме "
                "«Только для чтения» с категориями: Контент, Статистика, Вопросы и отзывы."
            ),
            ("<b>Где найти:</b> ЛК ВБ -> Интеграции по API -> Создать токен -> Для интеграции вручную."),
            ("<b>Зачем нужен токен:</b> для получения информации о товаре, проверки статуса заказов и отзывов."),
            ("<b>Безопасно:</b> токен создается только в режиме чтения, поэтому изменить данные с ним невозможно."),
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
        return self._get_seller_listing_creation_flow().instruction_text(shop_title=shop_title)

    def _parse_listing_create_input(
        self,
        text: str,
    ) -> tuple[int, Decimal, int, str, list[str]]:
        return parse_listing_create_csv(text)

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
        return self._get_seller_listing_creation_flow().title_edit_prompt_text(current_title=current_title)

    def _listing_title_confirmation_text(
        self,
        *,
        wb_product_id: int,
        search_phrase: str,
        review_phrases: list[str] | None,
        cashback_rub: Decimal,
        slot_count: int,
        snapshot: WbProductSnapshot,
        suggested_display_title: str,
        buyer_price_rub: int,
        reference_price_source: str,
        observed_buyer_price: WbObservedBuyerPrice | None = None,
    ) -> str:
        return self._get_seller_listing_creation_flow().title_confirmation_text(
            wb_product_id=wb_product_id,
            search_phrase=search_phrase,
            review_phrases=review_phrases,
            cashback_rub=cashback_rub,
            slot_count=slot_count,
            snapshot=snapshot,
            suggested_display_title=suggested_display_title,
            buyer_price_rub=buyer_price_rub,
            reference_price_source=reference_price_source,
            observed_buyer_price=observed_buyer_price,
        )

    def _listing_manual_price_prompt_text(
        self,
        *,
        wb_product_id: int,
        snapshot: WbProductSnapshot,
    ) -> str:
        return self._get_seller_listing_creation_flow().manual_price_prompt_text(
            wb_product_id=wb_product_id,
            snapshot=snapshot,
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
        new_collateral = (new_reward_usdt * Decimal(new_slot_count) * _LISTING_COLLATERAL_FEE_MULTIPLIER).quantize(
            _USDT_EXACT_QUANT, rounding=ROUND_HALF_UP
        )
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
                (f"<b>Название:</b> {html.escape(current_title)} -> {html.escape(new_display_title)}"),
                (
                    f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot; "
                    f"-> &quot;{html.escape(new_search_phrase)}&quot;"
                ),
                (
                    f"<b>Кэшбэк:</b> {self._format_usdt_with_rub(listing.reward_usdt)} "
                    f"-> {self._format_usdt_with_rub(new_reward_usdt)}"
                ),
                (f"<b>Кэшбэк, %:</b> {cashback_percent}"),
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
        review_phrases: list[str] | None,
        cashback_rub: Decimal,
        reward_usdt: Decimal,
        slot_count: int,
        collateral_required_usdt: Decimal,
    ) -> str:
        return self._get_seller_listing_creation_flow().created_prompt_activation_text(
            display_title=display_title,
            wb_product_id=wb_product_id,
            wb_subject_name=wb_subject_name,
            wb_vendor_code=wb_vendor_code,
            wb_source_title=wb_source_title,
            wb_brand_name=wb_brand_name,
            reference_price_rub=reference_price_rub,
            reference_price_source=reference_price_source,
            search_phrase=search_phrase,
            review_phrases=review_phrases,
            cashback_rub=cashback_rub,
            reward_usdt=reward_usdt,
            slot_count=slot_count,
            collateral_required_usdt=collateral_required_usdt,
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

        text = self._screen_text(
            title=f"Удаление магазина «{html.escape(shop.title)}» необратимо",
            lines=[
                f"Активных объявлений: {preview.active_listings_count}",
                f"Незавершенных покупок: {preview.open_assignments_count}",
                (
                    "Покупателям будет выплачен кэшбэк: "
                    f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}"
                ),
                (f"Продавцу вернется: {self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}"),
            ],
            note=("При удалении магазина незавершенные покупки закроются с выплатой кэшбэка покупателям."),
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
            shop_ref=self._shop_ref(shop_id),
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
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
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
        balance_snapshot = await self._seller_service.get_seller_balance_snapshot(
            seller_user_id=seller_user_id,
        )
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
                f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop_slug}" if shop_slug else "—"
            )
            collateral_line = self._format_listing_collateral_line(
                collateral_view=listing,
                seller_available_usdt=balance_snapshot.seller_available_usdt,
            )
            lines.append(
                f"<b>{number}. {html.escape(display_title)}</b>\n"
                f"<b>Артикул WB:</b> {listing.wb_product_id}\n"
                f"<b>Кэшбэк:</b> {cashback_text}\n"
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;\n"
                + (f"<b>План покупок / В процессе:</b> {listing.slot_count} / {listing.in_progress_assignments_count}")
                + "\n"
                + f"<b>Ссылка на магазин:</b> {html.escape(shop_link)}\n"
                + f"<b>Обеспечение:</b> {collateral_line}"
                + "\n"
                + (f"<b>Статус:</b> {self._listing_activity_badge(is_active=listing.status == 'active')}")
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
                note="Новое объявление создается кнопкой ниже.",
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
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                        )
                    ],
                    [self._knowledge_button(role=_ROLE_SELLER, topic="listings")],
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
        balance_snapshot = await self._seller_service.get_seller_balance_snapshot(
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
                seller_available_usdt=balance_snapshot.seller_available_usdt,
                shop_link=(f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"),
                notice=notice,
            ),
            self._seller_listing_detail_markup(
                listing_id=listing.listing_id,
                status=listing.status,
                list_page=list_page,
                can_activate=self._listing_has_sufficient_collateral(
                    collateral_view=collateral_view,
                    seller_available_usdt=balance_snapshot.seller_available_usdt,
                    listing_status=listing.status,
                ),
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
                review_phrases=list(prompt_state.get("review_phrases") or []),
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
                suggested_display_title=str(prompt_state.get("suggested_display_title", "")).strip(),
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
                review_phrases=list(prompt_state.get("review_phrases") or []),
                reference_price_rub=(
                    int(prompt_state["reference_price_rub"])
                    if prompt_state.get("reference_price_rub") is not None
                    else None
                ),
                reference_price_source=(str(prompt_state.get("reference_price_source", "")).strip() or None),
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
                review_phrases=getattr(listing, "review_phrases", []),
                cashback_rub=(listing.reward_usdt * self._display_rub_per_usdt).quantize(
                    _RUB_QUANT, rounding=ROUND_HALF_UP
                ),
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

        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        listing_counts_by_shop: dict[int, int] = {}
        for listing in listings:
            listing_counts_by_shop[listing.shop_id] = listing_counts_by_shop.get(listing.shop_id, 0) + 1
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text=self._button_label_with_count(
                        f"🏬 {shop.title}",
                        listing_counts_by_shop.get(shop.shop_id, 0),
                    ),
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
                cta="Выберите магазин для нового объявления.",
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
            workflow = self._seller_workflow_service
            if workflow is None:
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
            else:
                result = await workflow.activate_listing(
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
        self._logger.info(
            "seller_listing_activated",
            listing_id=listing_id,
            listing_ref=self._listing_ref(listing_id),
            changed=result.changed,
        )
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
        self._logger.info(
            "seller_listing_paused",
            listing_id=listing_id,
            listing_ref=self._listing_ref(listing_id),
            changed=result.changed,
        )
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
            workflow = self._seller_workflow_service
            if workflow is None:
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
            else:
                result = await workflow.unpause_listing(
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
        self._logger.info(
            "seller_listing_unpaused",
            listing_id=listing_id,
            listing_ref=self._listing_ref(listing_id),
            changed=result.changed,
        )
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
                f"Незавершенных покупок: {preview.open_assignments_count}",
                (
                    "Покупателям будет выплачен кэшбэк: "
                    f"{self._format_usdt_with_rub(preview.assignment_linked_reserved_usdt)}"
                ),
                (f"Продавцу вернется: {self._format_usdt_with_rub(preview.unassigned_collateral_usdt)}"),
            ],
            note=("При удалении объявления незавершенные покупки закроются с выплатой кэшбэка покупателям."),
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
            listing_ref=self._listing_ref(listing_id),
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
        snapshot = await self._seller_service.get_seller_balance_snapshot(seller_user_id=seller_user_id)
        active_request = await self._finance_service.get_active_seller_withdrawal_request(seller_user_id=seller_user_id)
        listings = await self._seller_service.list_listing_collateral_views(seller_user_id=seller_user_id)
        allocated_total = snapshot.seller_collateral_usdt
        required_total = sum((item.collateral_required_usdt for item in listings), Decimal("0"))
        activation_capacity = snapshot.seller_available_usdt + snapshot.seller_collateral_usdt
        shortfall = required_total - activation_capacity
        lines = [
            (f"<b>Свободно для новых объявлений:</b> {self._format_usdt_with_rub(snapshot.seller_available_usdt)}"),
            f"<b>Уже выделено под объявления:</b> {self._format_usdt_with_rub(allocated_total)}",
            (f"<b>В процессе вывода:</b> {self._format_usdt_with_rub(snapshot.seller_withdraw_pending_usdt)}"),
        ]
        if active_request is not None:
            withdraw_ref = self._withdrawal_ref(active_request.withdrawal_request_id)
            lines.append(
                "\n".join(
                    [
                        self._entity_block_heading_with_ref(label="Активная заявка", ref=withdraw_ref),
                        (f"<b>Сумма:</b> {self._format_usdt_value(active_request.amount_usdt, precise=True)} USDT"),
                        f"<b>Статус:</b> {self._withdraw_status_badge(active_request.status)}",
                        f"<b>Адрес:</b> {html.escape(active_request.payout_address)}",
                        f"<b>Создана:</b> {self._format_datetime_msk(active_request.requested_at)}",
                    ]
                )
            )
        if shortfall > Decimal("0.000000"):
            lines.append(f"<b>Не хватает для активации:</b> {self._format_usdt_with_rub(shortfall)}")
        text = self._screen_text(
            title="Баланс продавца",
            cta="Выберите следующее действие ниже.",
            lines=lines,
            note=(
                "Пополните баланс продавца, если средств не хватает для активации объявлений."
                if shortfall > Decimal("0.000000")
                else None
            ),
            separate_blocks=True,
        )
        await self._replace_message(
            query_message,
            text,
            self._seller_balance_menu_markup(
                can_withdraw_available=(
                    active_request is None and snapshot.seller_available_usdt > Decimal("0.000000")
                ),
                active_request_id=(active_request.withdrawal_request_id if active_request is not None else None),
            ),
            parse_mode="HTML",
        )

    async def _render_seller_transaction_history(
        self,
        *,
        query_message: Message | None,
        seller_user_id: int,
        page: int = 1,
    ) -> None:
        intents = await self._deposit_service.list_seller_deposit_intents(
            seller_user_id=seller_user_id,
            limit=1000,
        )
        withdrawals = await self._finance_service.list_seller_withdrawal_history(
            seller_user_id=seller_user_id,
            limit=1000,
        )
        combined_history: list[tuple[str, datetime, int, Any]] = []
        for item in intents:
            combined_history.append(
                (
                    "topup",
                    item.created_at,
                    int(getattr(item, "deposit_intent_id", 0) or 0),
                    item,
                )
            )
        for item in withdrawals:
            combined_history.append(
                (
                    "withdraw",
                    item.requested_at,
                    int(getattr(item, "withdrawal_request_id", 0) or 0),
                    item,
                )
            )
        combined_history.sort(key=lambda entry: (entry[1], entry[2]), reverse=True)

        if not combined_history:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Транзакции продавца",
                    cta="Здесь отображаются пополнения и выводы продавца.",
                    lines=["Транзакций пока нет."],
                    note="Нажмите «➕ Пополнить» или создайте заявку на вывод с экрана баланса.",
                ),
                self._seller_balance_menu_markup(),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(combined_history),
            requested_page=page,
            page_size=8,
        )
        lines: list[str] = []
        for entry_type, _, entry_id, item in combined_history[start_index:end_index]:
            if entry_type == "withdraw":
                withdraw_ref = self._withdrawal_ref(item.withdrawal_request_id)
                block_lines = [
                    self._entity_block_heading_with_ref(label="Вывод", ref=withdraw_ref),
                    f"<b>Сумма:</b> {self._format_usdt_value(item.amount_usdt, precise=True)} USDT",
                    f"<b>Статус:</b> {self._withdraw_status_badge(item.status)}",
                    f"<b>Адрес:</b> {html.escape(item.payout_address)}",
                    f"<b>Создана:</b> {self._format_datetime_msk(item.requested_at)}",
                ]
                if item.processed_at is not None:
                    block_lines.append(f"<b>Обработана:</b> {self._format_datetime_msk(item.processed_at)}")
                if item.sent_at is not None:
                    block_lines.append(f"<b>Отправлена:</b> {self._format_datetime_msk(item.sent_at)}")
                if item.note:
                    block_lines.append(f"<b>Комментарий:</b> {html.escape(item.note)}")
                if item.tx_hash:
                    block_lines.append(f"<b>Хэш перевода:</b> {html.escape(item.tx_hash)}")
                lines.append("\n".join(block_lines))
                continue

            expected_amount = self._format_usdt_value(item.expected_amount_usdt, precise=True)
            block_lines = []
            if entry_id > 0:
                deposit_ref = self._deposit_ref(entry_id)
                block_lines.append(self._entity_block_heading_with_ref(label="Счет на пополнение", ref=deposit_ref))
            else:
                block_lines.append("<b>Пополнение</b>")
            block_lines.extend(
                [
                    f"<b>Сумма:</b> {expected_amount} USDT",
                    f"<b>Статус:</b> {self._deposit_status_badge(item.status)}",
                    f"<b>Создан:</b> {self._format_datetime_msk(item.created_at)}",
                    f"<b>Срок счета:</b> до {self._format_datetime_msk(item.expires_at)}",
                ]
            )
            block = "\n".join(block_lines)
            if item.status == "credited" and item.credited_amount_usdt is not None:
                block += f"\n<b>Зачислено:</b> {self._format_usdt_value(item.credited_amount_usdt, precise=True)} USDT"
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
                cta="Проверьте статус пополнений и выводов ниже.",
                lines=lines,
                note=(
                    "Если пополнение или вывод зависли, проверьте статус "
                    "и при необходимости обратитесь к администратору."
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
                    "и отправьте точную сумму в сети TON.\n"
                    "Рекомендуем делать перевод на несколько объявлений сразу, "
                    "так как комиссия за перевод в сети TON составляет "
                    "фиксированный 1 USDT."
                ),
                lines=[
                    (
                        '1. Зайдите в <a href="https://help.ru.wallet.tg/article/60-znakomstvo-s-wallet">'
                        "официальный кошелек Wallet</a> в Telegram: "
                        '<a href="https://t.me/wallet">@wallet</a>.\n'
                        "Также можно использовать любой другой TON-совместимый кошелек "
                        "или перевести USDT напрямую с криптобиржи."
                    ),
                    (
                        "2. Пополните Крипто Кошелек, купив необходимый объем USDT, "
                        'например на <a href="https://help.ru.wallet.tg/article/'
                        '80-kak-kupit-kriptovalutu-na-p2p-markete">'
                        "P2P Маркете</a>.\n"
                        "Самый простой и быстрый способ: "
                        "Крипто Кошелек > Пополнить > P2P Экспресс."
                    ),
                    (
                        "3. Выведите USDT на предоставленный в боте адрес:\n"
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

    async def _start_seller_withdraw_full_amount(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        seller_user_id: int,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_SELLER,
            result=await self._seller_withdrawal_creation_flow().start_full_amount_prompt(
                requester_user_id=seller_user_id
            ),
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
                page=self._coerce_page_number(payload.entity_id),
            )
            return
        if action == "kb_guide":
            await self._render_buyer_knowledge_screen(query_message=query_message, topic="guide")
            return
        if action == "kb_shops":
            await self._render_buyer_knowledge_screen(query_message=query_message, topic="shops")
            return
        if action == "kb_purchases":
            await self._render_buyer_knowledge_screen(
                query_message=query_message,
                topic="purchases",
            )
            return
        if action == "kb_balance":
            await self._render_buyer_knowledge_screen(query_message=query_message, topic="balance")
            return
        if action == "shop_page":
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            if not slug:
                await self._render_buyer_shops_section(
                    query_message=query_message,
                    buyer_user_id=buyer.user_id,
                    notice="Магазин не найден. Выберите его из списка заново.",
                )
                return
            await self._send_buyer_shop_catalog(
                query_message,
                slug=slug,
                buyer_user_id=buyer.user_id,
                prefer_edit=True,
                page=self._coerce_page_number(payload.entity_id),
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
                    "Нет сохраненного магазина. Выберите магазин из списка.",
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
                    "Этот магазин больше недоступен. Выберите другой магазин.",
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
                page=1,
            )
            return
        if action == "shop_remove":
            if not payload.entity_id:
                await self._render_buyer_shops_section(
                    query_message=query_message,
                    buyer_user_id=buyer.user_id,
                    notice="Не удалось определить магазин. Выберите его заново.",
                )
                return
            await self._execute_buyer_saved_shop_remove(
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                shop_id=int(payload.entity_id),
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
                ("Введите код магазина из ссылки.\nЭто часть после shop_ в ссылке."),
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
                    "Не удалось открыть покупку. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
                self._screen_text(
                    title="Токен-подтверждение",
                    cta="Вставьте токен из расширения следующим сообщением ниже.",
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к покупкам",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action == "submit_review_payload_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть покупку. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
                prompt_type="buyer_submit_review_payload",
                sensitive=True,
                extra={"assignment_id": assignment_id},
            )
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Токен отзыва",
                    cta="Вставьте токен из расширения следующим сообщением ниже.",
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к покупкам",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action == "assignment_cancel_prompt":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось открыть покупку. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
            assignments = self._buyer_visible_assignments(
                await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer.user_id)
            )
            assignment = next(
                (item for item in assignments if item.assignment_id == assignment_id),
                None,
            )
            if assignment is None:
                await self._replace_message(
                    query_message,
                    "Покупка не найдена.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
            if assignment.status != "reserved":
                await self._replace_message(
                    query_message,
                    "Эту покупку уже нельзя отменить.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
                self._screen_text(
                    title="Отмена покупки",
                    cta="Подтвердите действие ниже.",
                    lines=["Бронь будет снята, а покупка снова станет доступна другим покупателям."],
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="✅ Отказаться от покупки",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignment_cancel_confirm",
                                    entity_id=str(assignment_id),
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к покупкам",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="assignments",
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )
            return
        if action == "assignment_cancel_confirm":
            if not payload.entity_id:
                await self._replace_message(
                    query_message,
                    "Не удалось отменить покупку. Попробуйте снова.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ Назад к покупкам",
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
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_withdrawal_creation_flow().start_manual_amount_prompt(
                    requester_user_id=buyer.user_id
                ),
            )
            return
        if action == "withdraw_cancel_prompt":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_withdrawal_creation_flow().start_cancel_prompt(
                    requester_user_id=buyer.user_id,
                    request_id=int(payload.entity_id) if payload.entity_id else None,
                ),
            )
            return
        if action == "withdraw_cancel_confirm":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_withdrawal_creation_flow().confirm_cancel(
                    requester_user_id=buyer.user_id,
                    request_id=int(payload.entity_id) if payload.entity_id else None,
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
        assignments = self._buyer_visible_assignments(
            await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        saved_shops = await self._buyer_service.list_saved_shops(buyer_user_id=buyer_user_id, limit=1000)
        snapshot = await self._finance_service.get_buyer_balance_snapshot(buyer_user_id=buyer_user_id)
        bucket_counts = {
            "awaiting_order": 0,
            "ordered": 0,
            "picked_up": 0,
        }
        for item in assignments:
            bucket = self._buyer_dashboard_status_bucket(item.status)
            if bucket is not None:
                bucket_counts[bucket] += 1
        text = self._screen_text(
            title="Кабинет покупателя",
            cta="Выберите раздел ниже.",
            lines=[
                (
                    "<b>Покупки:</b> "
                    f"ожидают заказа: {bucket_counts['awaiting_order']} · "
                    f"заказаны: {bucket_counts['ordered']} · "
                    f"выкуплены: {bucket_counts['picked_up']}"
                ),
                f"<b>Баланс:</b> {self._format_buyer_balance_amount(snapshot.buyer_available_usdt)}",
            ],
            separate_blocks=True,
        )
        await self._replace_message(
            query_message,
            text,
            self._buyer_menu_markup(
                shops_count=len(saved_shops),
                purchases_count=len(assignments),
            ),
            parse_mode="HTML",
        )

    async def _render_buyer_knowledge_screen(
        self,
        *,
        query_message: Message | None,
        topic: str,
    ) -> None:
        if topic == "guide":
            text = self._screen_text(
                title="Инструкция покупателя",
                cta=(
                    "Купилка позволяет просто и безопасно покупать товары на Wildberries "
                    "и получать за это кэшбэк на криптокошелек."
                ),
                lines=[
                    (
                        "<b>Как пользоваться ботом</b>\n"
                        "1. Установите расширение для браузера Chrome / Яндекс Qpilka "
                        "(обязательно):\n"
                        f'<a href="{_QPILKA_EXTENSION_URL}">{_QPILKA_EXTENSION_URL}</a>\n'
                        "2. Откройте магазин и выберите товар.\n"
                        "3. Нажмите «Купить» (произойдет бронирование товара) и скопируйте токен заявки на покупку.\n"
                        "4. Вставьте полученный токен в расширение браузера Qpilka и следуйте подсказкам "
                        "для совершения заказа товара. Важно это сделать в течение 4 часов, иначе "
                        "бронирование аннулируется.\n"
                        "5. Если вы все сделали правильно, то после заказа товара вы получите "
                        "токен-подтверждение. Отправьте его в бот.\n"
                        "6. Выкупите товар.\n"
                        "7. Отправьте (с использованием расширения для браузера) отзыв о товаре на 5 "
                        "звезд c упоминанием 1-2 характеристик (при наличии).\n"
                        "8. Дождитесь разблокировки кэшбэка через 15 дней после выкупа.\n"
                        "9. После начисления кэшбэка оформите вывод."
                    ),
                    (
                        "<b>FAQ</b>\n"
                        "1. <b>Где найти магазин?</b>\n"
                        "Ссылки на магазины публикуются в профильных телеграм группах.\n\n"
                        "2. <b>Что, если заказ не был сделан в течение 4 часов (бронь отменена)?</b>\n"
                        "Оформите новую покупку.\n\n"
                        "3. <b>Почему кэшбэк разблокируется только через 15 дней?</b>\n"
                        "В течение 14 дней покупатель может сделать возврат товара на маркетплейсе.\n\n"
                        "4. <b>Что будет, если я не выкуплю товар или верну его в течение 14 дней?</b>\n"
                        "Кэшбэк выплачен не будет.\n\n"
                        "5. <b>Где гарантия, что продавец выплатит кэшбэк?</b>\n"
                        "Сервис обеспечивает выплату: деньги замораживаются на счету продавца."
                    ),
                    (
                        "<b>Про суммы</b>\n"
                        "На экранах покупателя суммы обычно показаны как приблизительные значения в ₽. "
                        "Фактические расчеты и перевод в системе идут в USDT."
                    ),
                ],
                separate_blocks=True,
            )
            keyboard_rows = [
                [
                    self._knowledge_button(role=_ROLE_BUYER, topic="shops"),
                    self._knowledge_button(role=_ROLE_BUYER, topic="purchases"),
                ],
                [self._knowledge_button(role=_ROLE_BUYER, topic="balance")],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                    )
                ],
            ]
            support_button = self._build_support_button(role=_ROLE_BUYER)
            if support_button is not None:
                keyboard_rows.append([support_button])
            markup = InlineKeyboardMarkup(keyboard_rows)
        elif topic == "shops":
            text = self._screen_text(
                title="Про магазины",
                cta="Магазин — это подборка доступных объявлений одного продавца.",
                lines=[
                    (
                        "Магазины сохраняются в вашем профиле, и вы всегда можете к ним вернуться "
                        "позднее. Добавить магазин в профиль можно, перейдя по его ссылке."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Зеленая точка означает, что в магазине есть доступные объявления.\n"
                        "2. Удалить магазин нельзя, пока в нем есть незавершенная покупка.\n"
                        "3. Если активных объявлений нет, но покупка уже идет, пользуйтесь разделом «Покупки»."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_BUYER, topic="guide"),
                        self._knowledge_button(role=_ROLE_BUYER, topic="purchases"),
                    ],
                    [self._knowledge_button(role=_ROLE_BUYER, topic="balance")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К магазинам",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                        )
                    ],
                ]
            )
        elif topic == "purchases":
            text = self._screen_text(
                title="Про покупки",
                cta="Покупка появляется после бронирования товара и проходит несколько статусов.",
                lines=[
                    (
                        "Для получения кэшбэка на покупку необходимо:\n"
                        "- оформить заказ (с использованием расширения для браузера)\n"
                        "- сделать выкуп\n"
                        "- отправить отзыв о товаре на 5 звезд с упоминанием 1-2 характеристик "
                        "(с использованием расширения для браузера)\n"
                        "- подождать 15 дней после выкупа для разблокировки кэшбэка"
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Токен-подтверждение нужно отправить в течение 4 часов после брони.\n"
                        "3. От покупки можно отказаться, пока она еще не подтверждена.\n"
                        "4. Один и тот же товар нельзя брать повторно с одного аккаунта."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_BUYER, topic="guide"),
                        self._knowledge_button(role=_ROLE_BUYER, topic="shops"),
                    ],
                    [self._knowledge_button(role=_ROLE_BUYER, topic="balance")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К покупкам",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="assignments"),
                        )
                    ],
                ]
            )
        else:
            text = self._screen_text(
                title="Про баланс и вывод",
                cta=(
                    "На балансе покупателя отображается сумма, доступная к выводу, "
                    "а также сумма, ожидающая разблокировки кэшбэка."
                ),
                lines=[
                    (
                        "Суммы обычно показываются как приблизительные значения в ₽ для удобства. "
                        "Точная сумма вывода и все операции внутри системы ведутся в USDT."
                    ),
                    (
                        "<b>Полезно знать</b>\n"
                        "1. Вывод доступен только после разблокировки покупки.\n"
                        "2. Может быть только одна активная заявка на вывод.\n"
                        "3. Для вывода потребуется адрес кошелька в валюте USDT в сети TON."
                    ),
                ],
                separate_blocks=True,
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        self._knowledge_button(role=_ROLE_BUYER, topic="guide"),
                        self._knowledge_button(role=_ROLE_BUYER, topic="shops"),
                    ],
                    [self._knowledge_button(role=_ROLE_BUYER, topic="purchases")],
                    [
                        InlineKeyboardButton(
                            text="↩️ К балансу",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="balance"),
                        )
                    ],
                ]
            )
        await self._replace_message(query_message, text, markup, parse_mode="HTML")

    async def _render_buyer_shops_section(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        page: int = 1,
        notice: str | None = None,
    ) -> None:
        lines: list[str] = []
        saved_shops = await self._buyer_service.list_saved_shops(
            buyer_user_id=buyer_user_id,
            limit=100,
        )
        if notice:
            lines.append(html.escape(notice))
        if not saved_shops:
            text = self._screen_text(
                title="Магазины",
                cta="Сохраненных магазинов пока нет.",
                lines=lines,
                separate_blocks=True,
            )
            await self._replace_message(
                query_message,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад",
                                callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                            )
                        ],
                        [self._knowledge_button(role=_ROLE_BUYER, topic="shops")],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(saved_shops),
            requested_page=page,
        )
        shops_page = saved_shops[start_index:end_index]
        for idx, shop in enumerate(shops_page, start=start_index + 1):
            badge = self._buyer_shop_activity_badge(shop.active_listings_count)
            lines.append(f"<b>{idx}. {badge} {html.escape(shop.title)} (объявлений: {shop.active_listings_count})</b>")

        await self._replace_message(
            query_message,
            self._screen_text(
                title="Магазины",
                cta="Выберите номер магазина.",
                lines=lines,
                separate_blocks=True,
            ),
            self._numbered_page_markup(
                flow=_ROLE_BUYER,
                open_action="open_saved_shop",
                page_action="shops",
                item_ids=[shop.shop_id for shop in shops_page],
                start_number=start_index + 1,
                page=resolved_page,
                total_pages=total_pages,
                extra_rows=[
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                        )
                    ],
                    [self._knowledge_button(role=_ROLE_BUYER, topic="shops")],
                ],
            ),
            parse_mode="HTML",
        )

    async def _execute_buyer_saved_shop_remove(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        shop_id: int,
    ) -> None:
        try:
            shop = await self._buyer_service.resolve_saved_shop_for_buyer(
                buyer_user_id=buyer_user_id,
                shop_id=shop_id,
            )
        except NotFoundError:
            await self._render_buyer_shops_section(
                query_message=query_message,
                buyer_user_id=buyer_user_id,
                notice="Магазин уже удален из списка.",
            )
            return

        try:
            result = await self._buyer_service.remove_saved_shop(
                buyer_user_id=buyer_user_id,
                shop_id=shop_id,
            )
        except InvalidStateError:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title=f"Магазин «{html.escape(shop.title)}»",
                    cta="Удаление недоступно, пока в магазине есть незавершенная покупка.",
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="📋 Покупки",
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
            return

        if not result.changed:
            await self._render_buyer_shops_section(
                query_message=query_message,
                buyer_user_id=buyer_user_id,
                notice="Магазин уже удален из списка.",
            )
            return
        await self._render_buyer_shops_section(
            query_message=query_message,
            buyer_user_id=buyer_user_id,
            notice=f"Магазин «{shop.title}» удален из списка.",
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
            assignments = self._buyer_visible_assignments(
                await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
            )
            for item in assignments:
                if item.listing_id == listing_id and item.status not in {
                    "wb_invalid",
                    "returned_within_14d",
                    "delivery_expired",
                }:
                    active_same_listing = True
                    break

            if active_same_listing:
                await self._replace_message(
                    query_message,
                    "У вас уже есть активная покупка по этому товару.\nПродолжить можно в разделе «📋 Покупки».",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="📋 Покупки",
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
                "Свободных покупок по этому товару нет. Попробуйте выбрать другой товар.",
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
                    "У вас уже есть активная покупка по этому товару.\nПродолжить можно в разделе «📋 Покупки».",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="📋 Покупки",
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
                "Не удалось открыть покупку. Попробуйте снова.",
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

        assignments = self._buyer_visible_assignments(
            await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        assignment = next(
            (item for item in assignments if item.assignment_id == reservation.assignment_id),
            None,
        )
        if assignment is None:
            text = self._screen_text(
                title="Покупка создана",
                cta="Откройте раздел «📋 Покупки», чтобы продолжить.",
            )
        elif reservation.created:
            text = self._screen_text(
                title="Покупка создана",
                lines=[
                    self._buyer_task_instruction_text(assignment),
                ],
            )
        else:
            text = self._screen_text(
                title="Покупка уже активна",
                lines=[
                    self._buyer_task_instruction_text(assignment),
                ],
            )
        self._logger.info(
            "buyer_slot_reserved",
            listing_id=listing_id,
            listing_ref=self._listing_ref(listing_id),
            assignment_id=reservation.assignment_id,
            assignment_ref=self._assignment_ref(reservation.assignment_id),
            reservation_created=reservation.created,
        )
        keyboard_rows: list[list[InlineKeyboardButton]] = [
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
                    text="🚫 Отказаться от покупки",
                    callback_data=build_callback(
                        flow=_ROLE_BUYER,
                        action="assignment_cancel_prompt",
                        entity_id=str(reservation.assignment_id),
                    ),
                )
            ],
        ]
        keyboard_rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=self._button_label_with_count("📋 Покупки", len(assignments)),
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
        )
        keyboard_rows.append([self._knowledge_button(role=_ROLE_BUYER, topic="purchases")])
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(keyboard_rows),
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
        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="✅ Купить",
                    callback_data=build_callback(
                        flow=_ROLE_BUYER,
                        action="reserve",
                        entity_id=str(listing.listing_id),
                    ),
                )
            ]
        ]
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к каталогу",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="open_last_shop"),
                )
            ]
        )
        keyboard_rows.append([self._knowledge_button(role=_ROLE_BUYER, topic="purchases")])
        await self._replace_message(
            query_message,
            self._buyer_listing_detail_html(listing=listing, notice=notice),
            InlineKeyboardMarkup(keyboard_rows),
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
                idempotency_key=(f"tg-assignment-cancel:{buyer_user_id}:{assignment_id}:{callback_query_id}"),
            )
        except NotFoundError:
            await self._replace_message(
                query_message,
                "Покупка не найдена.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к покупкам",
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
                "Эту покупку уже нельзя отменить.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад к покупкам",
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
            "Покупка отменена. Она снова доступна другим покупателям."
            if result.changed
            else "Покупка уже была отменена ранее."
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="📋 Покупки",
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
        assignments = self._buyer_visible_assignments(
            await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
        )
        if not assignments:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Покупки",
                    cta="У вас пока нет покупок.",
                ),
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
                        ],
                        [self._knowledge_button(role=_ROLE_BUYER, topic="purchases")],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        lines: list[str] = []
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for item in assignments:
            display_title = self._listing_display_title(
                display_title=item.display_title,
                fallback=item.search_phrase,
            )
            shop_title = html.escape(self._buyer_shop_title(item))
            cashback_text = self._format_buyer_cashback_with_percent(
                reward_usdt=item.reward_usdt,
                reference_price_rub=item.reference_price_rub,
            )
            block_lines = [
                self._entity_block_heading_with_ref(
                    label="Покупка",
                    ref=self._assignment_ref(item.assignment_id),
                ),
                f"<b>Товар:</b> {html.escape(display_title)}",
                f"<b>Магазин:</b> {shop_title}",
                f"<b>Кэшбэк:</b> {cashback_text}",
            ]
            if item.order_id:
                block_lines.append(f"<b>Номер заказа:</b> {html.escape(item.order_id)}")
            block_lines.append(f"<b>Статус:</b> {self._buyer_purchase_status_badge(item.status)}")
            if item.status == "reserved":
                block_lines.append(self._buyer_task_instruction_text(item, include_title=False))
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
                            text="🚫 Отказаться от покупки",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="assignment_cancel_prompt",
                                entity_id=str(item.assignment_id),
                            ),
                        )
                    ]
                )
            elif item.status == "picked_up_wait_review":
                if getattr(item, "review_verification_status", None) == "pending_manual":
                    reason = str(getattr(item, "review_verification_reason", "") or "").strip()
                    if reason:
                        block_lines.append(
                            "<b>Проверка отзыва:</b> "
                            + html.escape(reason)
                            + " Исправьте отзыв или напишите в поддержку со скриншотом."
                        )
                    else:
                        block_lines.append(
                            "<b>Проверка отзыва:</b> "
                            "Автоматическая проверка не пройдена. "
                            "Исправьте отзыв или напишите в поддержку со скриншотом."
                        )
                block_lines.append(self._buyer_review_instruction_text(item, include_title=False))
                keyboard_rows.append(
                    [
                        InlineKeyboardButton(
                            text="✍️ Ввести токен отзыва",
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="submit_review_payload_prompt",
                                entity_id=str(item.assignment_id),
                            ),
                        )
                    ]
                )
            lines.append("\n".join(block_lines))
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="menu"),
                )
            ]
        )
        keyboard_rows.append([self._knowledge_button(role=_ROLE_BUYER, topic="purchases")])
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Покупки",
                cta="Проверьте статус покупок и выберите следующее действие ниже.",
                lines=lines,
                separate_blocks=True,
            ),
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
        snapshot = await self._finance_service.get_buyer_balance_snapshot(buyer_user_id=buyer_user_id)
        active_request = await self._finance_service.get_active_buyer_withdrawal_request(buyer_user_id=buyer_user_id)
        lines = [
            (f"<b>Доступно для вывода:</b> {self._format_buyer_balance_amount(snapshot.buyer_available_usdt)}"),
            (f"<b>В процессе вывода:</b> {self._format_buyer_balance_amount(snapshot.buyer_withdraw_pending_usdt)}"),
        ]
        if active_request is not None:
            withdraw_ref = self._withdrawal_ref(active_request.withdrawal_request_id)
            lines.append(
                "\n".join(
                    [
                        self._entity_block_heading_with_ref(label="Активная заявка", ref=withdraw_ref),
                        (f"<b>Сумма:</b> {self._format_usdt_value(active_request.amount_usdt, precise=True)} USDT"),
                        f"<b>Статус:</b> {self._withdraw_status_badge(active_request.status)}",
                        f"<b>Адрес:</b> {html.escape(active_request.payout_address)}",
                        f"<b>Создана:</b> {self._format_datetime_msk(active_request.requested_at)}",
                    ]
                )
            )
        text = self._screen_text(
            title="Баланс покупателя",
            cta="Выберите следующее действие ниже.",
            lines=lines,
            separate_blocks=True,
        )
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if active_request is not None:
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text="🚫 Отменить заявку",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="withdraw_cancel_prompt",
                            entity_id=str(active_request.withdrawal_request_id),
                        ),
                    )
                ]
            )
        elif snapshot.buyer_available_usdt > Decimal("0.000000"):
            keyboard_rows.extend(
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
                ]
            )
        keyboard_rows.extend(
            [
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
                [self._knowledge_button(role=_ROLE_BUYER, topic="balance")],
            ]
        )
        await self._replace_message(
            query_message,
            text,
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _start_withdraw_full_amount(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_withdrawal_creation_flow().start_full_amount_prompt(
                requester_user_id=buyer_user_id
            ),
        )

    async def _render_buyer_withdraw_history(
        self,
        *,
        query_message: Message | None,
        buyer_user_id: int,
        page: int = 1,
    ) -> None:
        total_items = await self._finance_service.count_buyer_withdrawal_history(buyer_user_id=buyer_user_id)
        if total_items < 1:
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
                        ],
                        [self._knowledge_button(role=_ROLE_BUYER, topic="balance")],
                    ]
                ),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=total_items,
            requested_page=page,
            page_size=8,
        )
        history = await self._finance_service.list_buyer_withdrawal_history(
            buyer_user_id=buyer_user_id,
            limit=end_index - start_index,
            offset=start_index,
        )
        lines: list[str] = []
        for item in history:
            withdraw_ref = self._withdrawal_ref(item.withdrawal_request_id)
            block_lines = [
                self._entity_block_heading_with_ref(label="Вывод", ref=withdraw_ref),
                f"<b>Сумма:</b> {self._format_usdt_value(item.amount_usdt, precise=True)} USDT",
                f"<b>Статус:</b> {self._withdraw_status_badge(item.status)}",
                f"<b>Адрес:</b> {html.escape(item.payout_address)}",
                f"<b>Создана:</b> {self._format_datetime_msk(item.requested_at)}",
            ]
            if item.processed_at is not None:
                block_lines.append(f"<b>Обработана:</b> {self._format_datetime_msk(item.processed_at)}")
            if item.sent_at is not None:
                block_lines.append(f"<b>Отправлена:</b> {self._format_datetime_msk(item.sent_at)}")
            if item.note:
                block_lines.append(f"<b>Комментарий:</b> {html.escape(item.note)}")
            if item.tx_hash:
                block_lines.append(f"<b>Хэш перевода:</b> {html.escape(item.tx_hash)}")
            lines.append("\n".join(block_lines))
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if resolved_page > 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="<",
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
                        text=">",
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
        keyboard_rows.append([self._knowledge_button(role=_ROLE_BUYER, topic="balance")])
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
                note=("Если вывод отклонен или задержан, проверьте статус и при необходимости оформите новую заявку."),
                separate_blocks=True,
            ),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _ensure_admin_user(self, *, telegram_id: int, username: str | None) -> int:
        if self._finance_service is None:
            raise RuntimeError("finance service is not initialized")
        return await self._finance_service.ensure_admin_user(
            telegram_id=telegram_id,
            username=username,
        )

    async def _render_admin_dashboard(self, *, query_message: Message | None) -> None:
        pending_withdrawals = await self._finance_service.list_pending_withdrawals(limit=1000)
        pending_review_confirmations = await self._buyer_service.list_admin_pending_review_confirmations(limit=1000)
        review_txs = await self._deposit_service.list_admin_review_txs(limit=1000)
        expired_intents = await self._deposit_service.list_admin_expired_intents(limit=1000)
        deposit_exceptions_count = len(review_txs) + len(expired_intents)
        exceptions_count = len(pending_review_confirmations) + deposit_exceptions_count

        text = self._screen_text(
            title="Кабинет администратора",
            cta="Выберите раздел ниже.",
            lines=[
                f"<b>Выводы в очереди:</b> {len(pending_withdrawals)}",
                f"<b>Отзывы на ручную проверку:</b> {len(pending_review_confirmations)}",
                f"<b>Платежи на ручной разбор:</b> {len(review_txs)}",
                f"<b>Просроченные счета:</b> {len(expired_intents)}",
            ],
            note="Откройте выводы, пополнения или исключения в зависимости от текущей задачи.",
        )
        await self._replace_message(
            query_message,
            text,
            self._admin_menu_markup(
                pending_withdrawals_count=len(pending_withdrawals),
                deposit_exceptions_count=deposit_exceptions_count,
                exceptions_count=exceptions_count,
            ),
            parse_mode="HTML",
        )

    async def _render_admin_withdrawals_section(self, *, query_message: Message | None) -> None:
        pending_count = len(await self._finance_service.list_pending_withdrawals(limit=1000))
        history_count = await self._finance_service.count_processed_withdrawals()
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Выводы",
                cta="Выберите действие ниже.",
                lines=["Раздел для обработки и проверки заявок на вывод."],
                note=("Откройте ожидающие заявки, историю или перейдите к конкретной заявке по коду или номеру."),
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text=self._button_label_with_count("📋 Ожидают обработки", pending_count),
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="withdrawals",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=self._button_label_with_count("🧾 История выводов", history_count),
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="withdrawals_history",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔎 Открыть заявку по коду",
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
        pending_reviews = await self._buyer_service.list_admin_pending_review_confirmations(limit=1000)
        review_txs = await self._deposit_service.list_admin_review_txs(limit=1000)
        expired_intents = await self._deposit_service.list_admin_expired_intents(limit=1000)
        exceptions_count = len(pending_reviews) + len(review_txs) + len(expired_intents)
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
                            text=self._button_label_with_count("⚠️ Нужна проверка", exceptions_count),
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
        pending = await self._finance_service.list_pending_withdrawals(limit=1000)
        if not pending:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="Ожидают обработки",
                    lines=["Очередь вывода пуста."],
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="🧾 История выводов",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_history",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔎 Открыть заявку по коду",
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
            withdraw_ref = self._withdrawal_ref(item.withdrawal_request_id)
            lines.append(
                f"{self._entity_block_heading_with_ref(label='Заявка', ref=withdraw_ref)}\n"
                f"Роль: {self._withdraw_requester_label(item.requester_role)}\n"
                f"Telegram: {item.requester_telegram_id} "
                f"(@{html.escape(item.requester_username or '-')})\n"
                f"Сумма: {self._format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
                f"Кошелек: {html.escape(item.payout_address)}"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🔎 Открыть {withdraw_ref}",
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
                    text="🧾 История выводов",
                    callback_data=build_callback(
                        flow=_ROLE_ADMIN,
                        action="withdrawals_history",
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
            self._screen_text(title="Ожидают обработки", lines=lines),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
        )

    async def _render_admin_processed_withdrawals(
        self,
        *,
        query_message: Message | None,
        page: int = 1,
    ) -> None:
        total_items = await self._finance_service.count_processed_withdrawals()
        if total_items < 1:
            await self._replace_message(
                query_message,
                self._screen_text(
                    title="История выводов",
                    lines=["Обработанных выводов пока нет."],
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="↩️ Назад",
                                callback_data=build_callback(
                                    flow=_ROLE_ADMIN,
                                    action="withdrawals_section",
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=total_items,
            requested_page=page,
            page_size=8,
        )
        history = await self._finance_service.list_processed_withdrawals(
            limit=end_index - start_index,
            offset=start_index,
        )
        lines: list[str] = []
        for item in history:
            withdraw_ref = self._withdrawal_ref(item.withdrawal_request_id)
            block_lines = [
                self._entity_block_heading_with_ref(label="Заявка", ref=withdraw_ref),
                f"Роль: {self._withdraw_requester_label(item.requester_role)}",
                (f"Telegram: {item.requester_telegram_id} (@{html.escape(item.requester_username or '-')})"),
                f"Сумма: {self._format_usdt_value(item.amount_usdt, precise=True)} USDT",
                f"Статус: {self._withdraw_status_badge(item.status)}",
                f"Кошелек: {html.escape(item.payout_address)}",
                f"Создана: {self._format_datetime_msk(item.requested_at)}",
            ]
            if item.processed_at:
                block_lines.append(f"Обработана: {self._format_datetime_msk(item.processed_at)}")
            if item.sent_at:
                block_lines.append(f"Отправлена: {self._format_datetime_msk(item.sent_at)}")
            if item.note:
                block_lines.append(f"Комментарий: {html.escape(item.note)}")
            if item.tx_hash:
                block_lines.append(f"Хэш перевода: {html.escape(item.tx_hash)}")
            lines.append("\n".join(block_lines))

        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if resolved_page > 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="<",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawals_history",
                            entity_id=str(resolved_page - 1),
                        ),
                    )
                )
            if resolved_page < total_pages:
                nav_row.append(
                    InlineKeyboardButton(
                        text=">",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawals_history",
                            entity_id=str(resolved_page + 1),
                        ),
                    )
                )
            if nav_row:
                keyboard_rows.append(nav_row)
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
            self._screen_text(
                title=(
                    f"История выводов · стр. {resolved_page}/{total_pages}" if total_pages > 1 else "История выводов"
                ),
                cta="Проверьте обработанные заявки ниже.",
                lines=lines,
                separate_blocks=True,
            ),
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
            detail = await self._finance_service.get_withdrawal_request_detail(request_id=request_id)
        except NotFoundError:
            await self._replace_message(
                query_message,
                "Заявка не найдена. Проверьте номер и попробуйте снова.",
            )
            return

        lines = [
            f"<b>Роль:</b> {self._withdraw_requester_label(detail.requester_role)}",
            f"<b>Telegram:</b> {detail.requester_telegram_id} (@{html.escape(detail.requester_username or '-')})",
            f"<b>Сумма:</b> {self._format_usdt_value(detail.amount_usdt, precise=True)} USDT",
            f"<b>Статус:</b> {self._withdraw_status_badge(detail.status)}",
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
                        text="✅ Подтвердить перевод",
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawal_complete_prompt",
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
                    text="↩️ К выводам",
                    callback_data=build_callback(flow=_ROLE_ADMIN, action="withdrawals_section"),
                )
            ]
        )
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Заявка",
                title_suffix_html=self._title_ref_suffix(self._withdrawal_ref(detail.withdrawal_request_id)),
                lines=lines,
            ),
            InlineKeyboardMarkup(keyboard_rows),
            parse_mode="HTML",
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
            result = await self._finance_service.reject_withdrawal_request(
                request_id=request_id,
                admin_user_id=admin_user_id,
                reason=reason,
                idempotency_key=f"tg-admin-reject:{admin_user_id}:{request_id}",
            )
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось отклонить заявку. Обновите список и попробуйте снова.",
            )
            return

        self._logger.info(
            "admin_withdraw_rejected",
            withdrawal_request_id=request_id,
            withdrawal_ref=self._withdrawal_ref(request_id),
            changed=result.changed,
        )
        await self._render_admin_withdrawal_detail(
            query_message=query_message,
            request_id=request_id,
        )

    async def _execute_admin_withdraw_complete(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        admin_user_id: int,
        request_id: int,
        tx_hash: str,
    ) -> bool:
        try:
            detail = await self._finance_service.get_withdrawal_request_detail(request_id=request_id)
            validation_error = await self._validate_withdrawal_completion_tx(
                tx_hash=tx_hash,
                payout_address=detail.payout_address,
                amount_usdt=detail.amount_usdt,
            )
            if validation_error is not None:
                await self._replace_message(
                    query_message,
                    validation_error,
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    text="↩️ К заявке",
                                    callback_data=build_callback(
                                        flow=_ROLE_ADMIN,
                                        action="withdrawal_detail",
                                        entity_id=str(request_id),
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return False
            system_payout_account_id = await self._ensure_system_payout_account_id()
            result = await self._finance_service.complete_withdrawal_request(
                request_id=request_id,
                admin_user_id=admin_user_id,
                system_payout_account_id=system_payout_account_id,
                tx_hash=tx_hash,
                idempotency_key=f"tg-admin-complete:{admin_user_id}:{request_id}",
            )
        except TonapiApiError:
            await self._replace_message(
                query_message,
                "Не удалось проверить перевод через TonAPI. Повторите попытку позже.",
            )
            return False
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось завершить заявку. Обновите список и попробуйте снова.",
            )
            return False

        self._logger.info(
            "admin_withdraw_completed",
            withdrawal_request_id=request_id,
            withdrawal_ref=self._withdrawal_ref(request_id),
            changed=result.changed,
        )
        await self._render_admin_withdrawal_detail(
            query_message=query_message,
            request_id=request_id,
        )
        return True

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
            tx_hash = external_reference[3:].strip() if external_reference.lower().startswith("tx:") else None
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
            self._logger.info(
                "admin_manual_deposit_user_notification_enqueued",
                target_telegram_id=target_telegram_id,
                amount_usdt=str(amount_usdt),
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
        pending_reviews = await self._buyer_service.list_admin_pending_review_confirmations(limit=1000)
        review_txs = await self._deposit_service.list_admin_review_txs(limit=1000)
        expired_intents = await self._deposit_service.list_admin_expired_intents(limit=1000)

        lines: list[str] = []
        if pending_reviews:
            lines.append("Отзывы, требующие проверки:")
            for item in pending_reviews[:20]:
                phrases_text = html.escape(self._format_review_phrases_text(item.review_phrases))
                lines.append(
                    f"Покупка {self._assignment_ref(item.assignment_id)}\n"
                    f"Покупатель: {item.buyer_telegram_id} "
                    f"(@{html.escape(item.buyer_username or '-')})\n"
                    f"Товар: {html.escape(item.display_title)}\n"
                    f"Оценка: {item.rating} / 5\n"
                    f"Фразы: {phrases_text}\n"
                    f"Причина: {html.escape(item.verification_reason or '-')}\n"
                    f"Текст: {html.escape(item.review_text)}"
                )
        else:
            lines.append("Отзывов на ручную проверку нет.")

        lines.append("⚠️ Пополнения, требующие проверки:")
        if review_txs:
            lines.append("Платежи на ручной разбор:")
            for tx in review_txs[:20]:
                suffix = f"{tx.suffix_code:03d}" if tx.suffix_code is not None else "нет"
                account_hint = (
                    f"Счет: {self._deposit_ref(tx.matched_intent_id)}" if tx.matched_intent_id else "Счет: не найден"
                )
                lines.append(
                    f"Транзакция {self._chain_tx_ref(tx.chain_tx_id)}\n"
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
            for intent in expired_intents[:20]:
                lines.append(
                    f"Счет {self._deposit_ref(intent.deposit_intent_id)}\n"
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
                        text=self._button_label_with_count("✅ Проверить отзыв", len(pending_reviews)),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="review_verify_prompt",
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=self._button_label_with_count(
                            "🔗 Привязать платеж к счету",
                            len(review_txs),
                        ),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposit_attach_prompt",
                        ),
                    ),
                    InlineKeyboardButton(
                        text=self._button_label_with_count("🛑 Отменить счет", len(expired_intents)),
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
        await self._replace_message(
            query_message,
            self._screen_text(
                title="Исключения",
                cta="Проверьте отзывы и пополнения, которым нужна ручная обработка.",
                lines=lines,
                separate_blocks=True,
            ),
            keyboard,
            parse_mode="HTML",
        )

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
                idempotency_key=(f"tg-admin-deposit-attach:{admin_user_id}:{chain_tx_id}:{deposit_intent_id}"),
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
                f"Счет: {self._deposit_ref(deposit_intent_id)}\n"
                f"Транзакция: {self._chain_tx_ref(chain_tx_id)}"
            )
        else:
            message = (
                "Эта операция уже была выполнена ранее.\n"
                f"Счет: {self._deposit_ref(deposit_intent_id)}\n"
                f"Транзакция: {self._chain_tx_ref(chain_tx_id)}"
            )
        await self._replace_message(query_message, message, self._admin_menu_markup())
        self._logger.info(
            "admin_deposit_attach_processed",
            chain_tx_id=chain_tx_id,
            chain_tx_ref=self._chain_tx_ref(chain_tx_id),
            deposit_intent_id=deposit_intent_id,
            deposit_ref=self._deposit_ref(deposit_intent_id),
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
            f"Счет {self._deposit_ref(deposit_intent_id)} отменен."
            if changed
            else f"Счет {self._deposit_ref(deposit_intent_id)} уже был отменен ранее."
        )
        await self._replace_message(query_message, message, self._admin_menu_markup())
        self._logger.info(
            "admin_deposit_cancel_processed",
            deposit_intent_id=deposit_intent_id,
            deposit_ref=self._deposit_ref(deposit_intent_id),
            changed=changed,
        )

    async def _execute_admin_review_verification(
        self,
        *,
        query_message: Message | None,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> None:
        try:
            result = await self._buyer_service.admin_verify_review_payload(
                admin_user_id=admin_user_id,
                assignment_id=assignment_id,
                payload_base64=payload_base64,
                idempotency_key=f"tg-admin-review-verify:{admin_user_id}:{assignment_id}",
            )
        except PayloadValidationError:
            await self._replace_message(
                query_message,
                (
                    "Не удалось подтвердить отзыв.\n"
                    "Проверьте, что токен относится к этой покупке и скопирован полностью."
                ),
                self._admin_menu_markup(),
            )
            return
        except (NotFoundError, InvalidStateError):
            await self._replace_message(
                query_message,
                "Не удалось подтвердить отзыв. Откройте исключения и попробуйте снова.",
                self._admin_menu_markup(),
            )
            return

        assignment_ref = self._assignment_ref(result.assignment_id)
        if result.changed:
            message = (
                "Отзыв подтвержден вручную.\n"
                f"Покупка: {assignment_ref}\n"
                "Кэшбэк будет разблокирован по стандартному сроку после выкупа."
            )
        else:
            message = f"Отзыв для покупки {assignment_ref} уже был подтвержден ранее."
        await self._replace_message(query_message, message, self._admin_menu_markup())
        self._logger.info(
            "admin_review_verified",
            assignment_id=result.assignment_id,
            assignment_ref=assignment_ref,
            changed=result.changed,
            verification_status=result.verification_status,
        )

    async def _resolve_manual_deposit_target(
        self,
        *,
        target_telegram_id: int,
        account_kind: str,
    ) -> tuple[int, int]:
        if self._finance_service is None:
            raise RuntimeError("finance service is not initialized")
        return await self._finance_service.resolve_manual_deposit_target(
            target_telegram_id=target_telegram_id,
            account_kind=account_kind,
        )

    async def _ensure_system_payout_account_id(self) -> int:
        if self._finance_service is None:
            raise RuntimeError("finance service is not initialized")
        return await self._finance_service.ensure_system_account_id(account_kind="system_payout")

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

    async def _notification_dispatch_loop(self, *, bot) -> None:
        while True:
            try:
                await self._dispatch_notifications_once(bot=bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.exception(
                    "notification_dispatch_loop_failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                )
            await asyncio.sleep(_NOTIFICATION_DISPATCH_POLL_SECONDS)

    async def _dispatch_notifications_once(self, *, bot) -> None:
        if self._notification_service is None:
            return
        await self._refresh_display_rub_per_usdt()
        items = await self._notification_service.claim_pending(limit=_NOTIFICATION_DISPATCH_BATCH_SIZE)
        for item in items:
            try:
                rendered = render_telegram_notification(
                    item,
                    display_rub_per_usdt=self._display_rub_per_usdt,
                )
                await bot.send_message(
                    chat_id=item.recipient_telegram_id,
                    text=rendered.text,
                    reply_markup=self._notification_markup(rendered),
                    parse_mode=rendered.parse_mode,
                )
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                await self._notification_service.mark_failed_permanent(
                    notification_id=item.notification_id,
                    error=str(exc),
                )
                self._logger.warning(
                    "notification_delivery_failed_render",
                    notification_id=item.notification_id,
                    telegram_id=item.recipient_telegram_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:300],
                )
                continue
            except Exception as exc:
                if self._is_permanent_notification_error(exc):
                    await self._notification_service.mark_failed_permanent(
                        notification_id=item.notification_id,
                        error=str(exc),
                    )
                    self._logger.warning(
                        "notification_delivery_failed_permanent",
                        notification_id=item.notification_id,
                        telegram_id=item.recipient_telegram_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:300],
                    )
                else:
                    retry_delay = self._notification_retry_delay(item.attempt_count + 1)
                    await self._notification_service.mark_retry(
                        notification_id=item.notification_id,
                        error=str(exc),
                        delay_seconds=retry_delay,
                    )
                    self._logger.warning(
                        "notification_delivery_failed_retry",
                        notification_id=item.notification_id,
                        telegram_id=item.recipient_telegram_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:300],
                        retry_delay_seconds=retry_delay,
                    )
                continue
            await self._notification_service.mark_sent(notification_id=item.notification_id)
            self._logger.info(
                "notification_delivered",
                notification_id=item.notification_id,
                telegram_id=item.recipient_telegram_id,
                event_type=item.event_type,
            )

    def _notification_markup(self, rendered) -> InlineKeyboardMarkup | None:
        if not rendered.cta_flow or not rendered.cta_action or not rendered.cta_text:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=rendered.cta_text,
                        callback_data=build_callback(
                            flow=rendered.cta_flow,
                            action=rendered.cta_action,
                            entity_id=rendered.cta_entity_id or "",
                        ),
                    )
                ]
            ]
        )

    @staticmethod
    def _notification_retry_delay(attempt_number: int) -> int:
        delay = min(_NOTIFICATION_DISPATCH_MAX_BACKOFF_SECONDS, 30 * (2 ** max(0, attempt_number - 1)))
        return int(delay)

    @staticmethod
    def _is_permanent_notification_error(exc: Exception) -> bool:
        error_type = type(exc).__name__
        message = str(exc).lower()
        return error_type == "Forbidden" or "chat not found" in message

    @staticmethod
    def _withdraw_requester_label(requester_role: str) -> str:
        return {
            "buyer": "Покупатель",
            "seller": "Продавец",
        }.get(requester_role, requester_role)

    def _withdraw_status_message(
        self,
        *,
        requester_role: str,
        request_id: int,
        status: str,
        reason: str | None = None,
        tx_hash: str | None = None,
    ) -> str:
        subject = "Заявка продавца на вывод" if requester_role == "seller" else "Ваша заявка на вывод"
        withdraw_ref = self._withdrawal_ref(request_id)
        if status == "rejected":
            message = f"{subject} {withdraw_ref} отклонена."
            if reason:
                message += f"\nПричина: {reason}"
            return message
        if status == "withdraw_sent":
            message = f"{subject} {withdraw_ref} отправлена."
            if tx_hash:
                message += f"\nХэш перевода: {tx_hash}"
            return message
        return f"{subject} {withdraw_ref}: {self._humanize_withdraw_status(status)}."

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
        if action == "withdrawals_history":
            await self._render_admin_processed_withdrawals(
                query_message=query_message,
                page=self._coerce_page_number(payload.entity_id),
            )
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
                f"Введите причину отклонения для заявки {self._withdrawal_ref(request_id)}.",
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
        if action == "withdrawal_complete_prompt":
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
                (
                    f"Введите хэш перевода для заявки {self._withdrawal_ref(request_id)}. "
                    "Заявка будет завершена только после проверки tx в сети TON USDT."
                ),
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
                "Введите код или номер заявки на вывод, например W77 или 77.",
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
        if action == "review_verify_prompt":
            self._set_prompt(
                context,
                role=_ROLE_ADMIN,
                prompt_type="admin_review_verify",
                sensitive=True,
                extra={"admin_user_id": admin_user_id},
            )
            await self._replace_message(
                query_message,
                "Введите: <код_покупки> <base64_review_token>.\nНапример: P31 eyJ...==",
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
                "Введите: <код_транзакции> <код_счета>.\nНапример: TX11 D22",
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
                "Введите: <код_счета> <причина>.\nНапример: D22 late_payment",
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
                    ("Не удалось продолжить создание магазина. Начните заново из раздела «🏪 Магазины»."),
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
                    error_text = "Магазин с таким названием уже есть.\nВведите другое название."
                else:
                    error_text = "Не удалось создать магазин.\nПроверьте название и попробуйте еще раз."
                await message.reply_text(
                    error_text,
                    reply_markup=self._seller_back_markup(action="shops", label="↩️ К магазинам"),
                )
                return

            deep_link = f"https://t.me/{self._settings.telegram_bot_username}?start=shop_{shop.slug}"
            self._clear_prompt(context)
            await message.reply_text(
                (f"Магазин «{shop.title}» создан.\nСсылка для покупателей:\n{deep_link}"),
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
                await message.reply_text("Не удалось продолжить ввод токена. Откройте карточку магазина заново.")
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
                    notice=("Не удалось проверить или сохранить токен. Попробуйте снова через карточку магазина."),
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
                    error_text = "Магазин с таким названием уже существует.\nВведите другое название."
                else:
                    error_text = "Не удалось переименовать магазин.\nПроверьте название и попробуйте еще раз."
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
                (f"Магазин переименован: «{shop.title}».\nНовая ссылка для покупателей:\n{deep_link}"),
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
            if seller_user_id < 1 or shop_id < 1:
                self._clear_prompt(context)
                await message.reply_text(
                    ("Не удалось продолжить создание объявления. Откройте раздел «📦 Объявления» заново."),
                    reply_markup=self._seller_back_markup(action="listings", label="↩️ К объявлениям"),
                )
                return
            result = await self._get_seller_listing_creation_flow().submit_listing_input(
                seller_user_id=seller_user_id,
                shop_id=shop_id,
                shop_title=shop_title,
                text=text,
            )
            await self._apply_transport_effects(
                context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=result,
            )
            return

        if prompt_type == "seller_listing_manual_price":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=self._get_seller_listing_creation_flow().submit_manual_price(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_listing_title_edit":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=self._get_seller_listing_creation_flow().submit_edited_title(
                    prompt_state=prompt_state,
                    text=text,
                ),
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

        if prompt_type == "seller_withdraw_amount":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_withdrawal_creation_flow().submit_manual_amount(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_withdraw_address":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_withdrawal_creation_flow().submit_address(
                    prompt_state=prompt_state,
                    text=text,
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                    update_id=update.update_id,
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
                (shard for shard in shards if shard.shard_key == self._settings.seller_collateral_shard_key),
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
                        ("Сейчас нельзя создать новый счет: достигнут лимит активных счетов.\nПопробуйте позже."),
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
                    title_suffix_html=self._title_ref_suffix(self._deposit_ref(intent.deposit_intent_id)),
                    cta=(
                        "Откройте Телеграм Кошелек или используйте ссылку для других "
                        "кошельков, либо скопируйте адрес и сумму вручную."
                    ),
                    lines=[
                        (f"<b>Срок действия:</b> {self._settings.seller_collateral_invoice_ttl_hours} ч"),
                        "<b>Сеть:</b> USDT в сети TON (не ERC-20)",
                        f"<b>Адрес:</b> {self._format_copyable_code(intent.deposit_address)}",
                        (f"<b>Сумма (должна полностью совпадать):</b> {expected_amount_text}"),
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
                                    text=f"QPI deposit {self._deposit_ref(intent.deposit_intent_id)}",
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
                await message.reply_text("Покупка не найдена. Откройте список покупок заново.")
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
                await message.reply_text("Покупка не найдена.")
                return
            except PayloadValidationError as exc:
                details = str(exc).strip().lower()
                base = (
                    "Токен-подтверждение не принят.\n"
                    "Проверьте, что вы скопировали его полностью из расширения для этой покупки."
                )
                if any(token in details for token in ("task_uuid", "wb_product_id", "token_type")):
                    await message.reply_text(f"{base}\nПохоже, токен относится к другой покупке или устарел.")
                elif details and "timezone" in details:
                    await message.reply_text(
                        f"{base}\nПроверьте дату и время на устройстве и сформируйте токен заново."
                    )
                else:
                    await message.reply_text(base)
                return
            except DuplicateOrderError:
                await message.reply_text("Этот номер заказа уже использован в другой покупке.")
                return
            except InvalidStateError:
                await message.reply_text("Сейчас нельзя отправить токен-подтверждение для этой покупки.")
                return

            self._clear_prompt(context)
            if result.changed:
                reply = (
                    "Токен-подтверждение принят.\n"
                    f"Номер заказа: {result.order_id}\n"
                    "Дальше мы автоматически проверим выкуп и начисление кэшбэка."
                )
            else:
                reply = f"Этот токен-подтверждение уже отправлен ранее.\nНомер заказа: {result.order_id}"
            self._logger.info(
                "buyer_payload_submitted",
                telegram_update_id=update.update_id,
                assignment_id=result.assignment_id,
                assignment_ref=self._assignment_ref(result.assignment_id),
                changed=result.changed,
            )
            await message.reply_text(reply, reply_markup=self._buyer_menu_markup())
            return

        if prompt_type == "buyer_submit_review_payload":
            assignment_id = int(prompt_state.get("assignment_id", 0))
            if assignment_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Покупка не найдена. Откройте список покупок заново.")
                return
            try:
                buyer = await self._buyer_service.bootstrap_buyer(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                )
                result = await self._buyer_service.submit_review_payload(
                    buyer_user_id=buyer.user_id,
                    assignment_id=assignment_id,
                    payload_base64=text,
                )
            except NotFoundError:
                await message.reply_text("Покупка не найдена.")
                return
            except PayloadValidationError as exc:
                details = str(exc).strip().lower()
                base = (
                    "Токен отзыва не принят.\n"
                    "Проверьте, что вы скопировали его полностью из расширения для этой покупки."
                )
                if any(token in details for token in ("task_uuid", "wb_product_id", "token_type")):
                    await message.reply_text(f"{base}\nПохоже, токен относится к другой покупке или устарел.")
                elif "timezone" in details:
                    await message.reply_text(
                        f"{base}\nПроверьте дату и время на устройстве и сформируйте токен заново."
                    )
                else:
                    await message.reply_text(base)
                return
            except InvalidStateError:
                await message.reply_text("Сейчас нельзя отправить токен отзыва для этой покупки.")
                return

            self._clear_prompt(context)
            reply_markup = self._buyer_menu_markup()
            if result.verification_status != "pending_manual":
                if result.changed:
                    reply = "Отзыв подтвержден. Ожидайте начисления кэшбэка через 15 дней после выкупа товара."
                else:
                    reply = "Этот токен отзыва уже был отправлен ранее."
            else:
                reason = str(result.verification_reason or "").strip()
                if result.changed:
                    reply = (
                        "Токен отзыва сохранен, но автоматическая проверка не пройдена.\nКэшбэк пока не будет выплачен."
                    )
                else:
                    reply = "Этот токен отзыва уже был отправлен ранее.\nКэшбэк по покупке все еще заблокирован."
                if reason:
                    reply += f"\nПричина: {reason}"
                reply += (
                    "\nИсправьте отзыв и отправьте новый токен "
                    "или напишите в поддержку со скриншотом опубликованного отзыва."
                )
                reply_markup = self._buyer_review_followup_markup(assignment_id=result.assignment_id)
            self._logger.info(
                "buyer_review_payload_submitted",
                telegram_update_id=update.update_id,
                assignment_id=result.assignment_id,
                assignment_ref=self._assignment_ref(result.assignment_id),
                changed=result.changed,
                verification_status=result.verification_status,
            )
            await message.reply_text(reply, reply_markup=reply_markup)
            return

        if prompt_type == "buyer_withdraw_amount":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_BUYER,
                result=await self._buyer_withdrawal_creation_flow().submit_manual_amount(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "buyer_withdraw_address":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_BUYER,
                result=await self._buyer_withdrawal_creation_flow().submit_address(
                    prompt_state=prompt_state,
                    text=text,
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                    update_id=update.update_id,
                ),
            )
            return

        if prompt_type == "admin_request_id":
            request_id_raw = text.strip()
            try:
                request_id = self._parse_withdrawal_reference(request_id_raw)
            except ValueError:
                await message.reply_text("Код заявки должен быть вида W77 или числом.")
                return
            self._clear_prompt(context)
            await self._render_admin_withdrawal_detail(
                query_message=message,
                request_id=request_id,
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
            completed = await self._execute_admin_withdraw_complete(
                context=context,
                query_message=message,
                admin_user_id=admin_user_id,
                request_id=request_id,
                tx_hash=tx_hash,
            )
            if completed:
                self._clear_prompt(context)
            return

        if prompt_type == "admin_manual_deposit":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=3)
            if len(tokens) != 4:
                await message.reply_text("Формат:\n<telegram_id> <роль> <сумма_usdt> <комментарий_или_ссылка>")
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

        if prompt_type == "admin_review_verify":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=1)
            if len(tokens) != 2:
                await message.reply_text("Формат: <код_покупки> <base64_review_token>")
                return
            assignment_raw, payload_base64 = tokens
            try:
                assignment_id = self._parse_assignment_reference(assignment_raw)
            except ValueError:
                await message.reply_text("Используйте код покупки вида P31 или обычное число.")
                return
            if admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста админа. Откройте меню заново.")
                return
            self._clear_prompt(context)
            await self._execute_admin_review_verification(
                query_message=message,
                admin_user_id=admin_user_id,
                assignment_id=assignment_id,
                payload_base64=payload_base64.strip(),
            )
            return

        if prompt_type == "admin_deposit_attach":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=1)
            if len(tokens) != 2:
                await message.reply_text("Формат: <код_транзакции> <код_счета>")
                return
            chain_tx_raw, intent_raw = tokens
            try:
                chain_tx_id = self._parse_chain_tx_reference(chain_tx_raw)
                deposit_intent_id = self._parse_deposit_reference(intent_raw)
            except ValueError:
                await message.reply_text("Используйте коды вида TX11 D22 или обычные числа.")
                return
            if admin_user_id < 1:
                self._clear_prompt(context)
                await message.reply_text("Ошибка контекста админа. Откройте меню заново.")
                return
            self._clear_prompt(context)
            await self._execute_admin_deposit_attach(
                query_message=message,
                admin_user_id=admin_user_id,
                chain_tx_id=chain_tx_id,
                deposit_intent_id=deposit_intent_id,
            )
            return

        if prompt_type == "admin_deposit_cancel":
            admin_user_id = int(prompt_state.get("admin_user_id", 0))
            tokens = text.split(maxsplit=1)
            if len(tokens) != 2:
                await message.reply_text("Формат: <код_счета> <причина>")
                return
            intent_raw, reason = tokens
            try:
                deposit_intent_id = self._parse_deposit_reference(intent_raw)
            except ValueError:
                await message.reply_text("Код счета должен быть вида D22 или числом.")
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
                deposit_intent_id=deposit_intent_id,
                reason=reason,
            )
            return

        self._clear_prompt(context)
        await message.reply_text("Неизвестный тип ввода. Отправьте /start.")

    def _get_seller_listing_creation_flow(self) -> SellerListingCreationFlow:
        if self._seller_listing_creation_flow is not None:
            return self._seller_listing_creation_flow
        seller_workflow = self._seller_workflow_service or _RuntimeSellerListingWorkflowAdapter(self)
        self._seller_listing_creation_flow = SellerListingCreationFlow(
            seller_service=self._seller_service,
            seller_workflow=seller_workflow,
            display_rub_per_usdt=self._display_rub_per_usdt,
            fx_rate_service=self._fx_rate_service,
            fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
        )
        return self._seller_listing_creation_flow

    def _seller_withdrawal_creation_flow(self) -> WithdrawalRequestCreationFlow:
        return WithdrawalRequestCreationFlow(
            config=SELLER_WITHDRAWAL_CONFIG,
            requester_adapter=_RuntimeSellerWithdrawalAdapter(self),
            address_validator=_RuntimeTonMainnetAddressValidator(self),
        )

    def _buyer_withdrawal_creation_flow(self) -> WithdrawalRequestCreationFlow:
        return WithdrawalRequestCreationFlow(
            config=BUYER_WITHDRAWAL_CONFIG,
            requester_adapter=_RuntimeBuyerWithdrawalAdapter(self),
            address_validator=_RuntimeTonMainnetAddressValidator(self),
        )

    async def _apply_transport_effects(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        query_message: Message | None,
        message: Message | None,
        result: FlowResult,
        default_role: str,
        callback_query: Any | None = None,
    ) -> None:
        for effect in result.effects:
            if isinstance(effect, SetPrompt):
                self._set_prompt(
                    context,
                    role=effect.role or default_role,
                    prompt_type=effect.prompt_type,
                    sensitive=effect.sensitive,
                    extra=effect.data,
                )
                continue
            if isinstance(effect, ClearPrompt):
                self._clear_prompt(context)
                continue
            if isinstance(effect, AnswerCallback):
                if callback_query is not None:
                    await callback_query.answer(
                        text=effect.text,
                        show_alert=effect.show_alert,
                    )
                continue
            if isinstance(effect, DeleteSourceMessage):
                target = message or query_message
                if target is not None:
                    await self._delete_sensitive_message(target, notify=False)
                continue
            if isinstance(effect, ReplyPhoto):
                await self._reply_with_photo_if_available(
                    message or query_message,
                    photo_url=effect.photo_url,
                )
                continue
            if isinstance(effect, ReplyText):
                target = message or query_message
                if target is not None:
                    await target.reply_text(
                        effect.text,
                        reply_markup=self._flow_buttons_markup(effect.buttons),
                        parse_mode=effect.parse_mode,
                    )
                continue
            if isinstance(effect, ReplyRoleMenuText):
                target = message or query_message
                if target is not None:
                    await target.reply_text(
                        effect.text,
                        reply_markup=self._role_menu_markup(effect.role),
                        parse_mode=effect.parse_mode,
                    )
                continue
            if isinstance(effect, ReplaceText):
                await self._replace_message(
                    query_message or message,
                    effect.text,
                    self._flow_buttons_markup(effect.buttons),
                    parse_mode=effect.parse_mode,
                )
                continue
            if isinstance(effect, LogEvent):
                self._logger.info(effect.event_name, **effect.fields)

    def _role_menu_markup(self, role: str) -> InlineKeyboardMarkup:
        if role == _ROLE_SELLER:
            return self._seller_menu_markup()
        if role == _ROLE_BUYER:
            return self._buyer_menu_markup()
        if role == _ROLE_ADMIN:
            return self._admin_menu_markup()
        raise ValueError(f"unsupported role menu: {role}")

    def _flow_buttons_markup(
        self,
        rows: tuple[tuple[ButtonSpec, ...], ...],
    ) -> InlineKeyboardMarkup | None:
        if not rows:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=button.text,
                        url=button.url,
                        callback_data=(
                            None
                            if button.url
                            else build_callback(
                                flow=str(button.flow),
                                action=str(button.action),
                                entity_id=button.entity_id,
                            )
                        ),
                    )
                    for button in row
                ]
                for row in rows
            ]
        )

    async def _send_buyer_shop_catalog(
        self,
        message: Message | None,
        *,
        slug: str,
        buyer_user_id: int | None = None,
        prefer_edit: bool = False,
        page: int = 1,
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

        active_shop_purchase = None
        active_shop_purchases_count = 0
        if buyer_user_id is not None:
            buyer_assignments = self._buyer_visible_assignments(
                await self._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)
            )
            active_shop_purchases_count = sum(1 for item in buyer_assignments if item.shop_slug == shop.slug)
            active_shop_purchase = next(
                (item for item in buyer_assignments if item.shop_slug == shop.slug),
                None,
            )

        can_remove_shop = active_shop_purchase is None and buyer_user_id is not None
        header = f"Магазин «{shop.title}»"
        shop_ref = self._shop_ref(shop.shop_id)
        if not listings:
            if active_shop_purchase is not None:
                text = self._screen_text(
                    title=html.escape(header),
                    title_suffix_html=self._title_ref_suffix(shop_ref),
                    cta=("У вас уже есть активная покупка в этом магазине. Других объявлений здесь пока нет."),
                )
                keyboard_rows = [
                    [
                        InlineKeyboardButton(
                            text=self._button_label_with_count("📋 Покупки", active_shop_purchases_count),
                            callback_data=build_callback(
                                flow=_ROLE_BUYER,
                                action="assignments",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="↩️ Назад к магазинам",
                            callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                        )
                    ],
                    [self._knowledge_button(role=_ROLE_BUYER, topic="shops")],
                ]
            else:
                text = self._screen_text(
                    title=html.escape(header),
                    title_suffix_html=self._title_ref_suffix(shop_ref),
                    cta="Активных объявлений пока нет.",
                )
                keyboard_rows: list[list[InlineKeyboardButton]] = []
                if can_remove_shop:
                    keyboard_rows.append(
                        [
                            InlineKeyboardButton(
                                text="🗑 Удалить магазин",
                                callback_data=build_callback(
                                    flow=_ROLE_BUYER,
                                    action="shop_remove",
                                    entity_id=str(shop.shop_id),
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
                keyboard_rows.append([self._knowledge_button(role=_ROLE_BUYER, topic="shops")])
            markup = InlineKeyboardMarkup(keyboard_rows)
            if prefer_edit:
                await self._replace_message(message, text, markup, parse_mode="HTML")
            elif message is not None:
                await message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return

        resolved_page, total_pages, start_index, end_index = self._resolve_numbered_page(
            total_items=len(listings),
            requested_page=page,
        )
        listings_page = listings[start_index:end_index]
        lines: list[str] = []
        for idx, listing in enumerate(listings_page, start=start_index + 1):
            display_title = self._listing_display_title(
                display_title=listing.display_title,
                fallback=listing.search_phrase,
            )
            cashback_text = self._format_buyer_cashback_with_percent(
                reward_usdt=listing.reward_usdt,
                reference_price_rub=listing.reference_price_rub,
            )
            lines.append(
                f"<b>{idx}. {html.escape(display_title)}</b>\n"
                f"<b>Цена:</b> {self._format_price_optional_rub(listing.reference_price_rub)}\n"
                f"<b>Кэшбэк:</b> {cashback_text}"
            )
        extra_rows: list[list[InlineKeyboardButton]] = []
        if can_remove_shop:
            extra_rows.append(
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить магазин",
                        callback_data=build_callback(
                            flow=_ROLE_BUYER,
                            action="shop_remove",
                            entity_id=str(shop.shop_id),
                        ),
                    )
                ]
            )
        extra_rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="↩️ Назад к магазинам",
                        callback_data=build_callback(flow=_ROLE_BUYER, action="shops"),
                    )
                ],
                [self._knowledge_button(role=_ROLE_BUYER, topic="shops")],
            ]
        )
        text = self._screen_text(
            title=html.escape(header),
            title_suffix_html=self._title_ref_suffix(shop_ref),
            cta="Выберите номер объявления.",
            lines=lines,
            separate_blocks=True,
        )
        markup = self._numbered_page_markup(
            flow=_ROLE_BUYER,
            open_action="listing_open",
            page_action="shop_page",
            item_ids=[listing.listing_id for listing in listings_page],
            start_number=start_index + 1,
            page=resolved_page,
            total_pages=total_pages,
            extra_rows=extra_rows,
        )
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
        try:
            await message.delete()
        except Exception as exc:
            self._logger.warning(
                "telegram_sensitive_delete_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
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
        return self._format_buyer_cashback_with_percent(
            reward_usdt=amount,
            reference_price_rub=None,
        )

    def _format_buyer_cashback_with_percent(
        self,
        *,
        reward_usdt: Decimal,
        reference_price_rub: int | None,
    ) -> str:
        primary = self._format_rub_approx(reward_usdt)
        if reward_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) == Decimal("0.000000"):
            return primary
        if reference_price_rub is None or reference_price_rub < 1:
            return primary
        cashback_rub = Decimal(self._format_cashback_rub_value(reward_usdt))
        percent = (cashback_rub / Decimal(reference_price_rub) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
        return f"{primary} (~{percent}%)"

    def _format_buyer_balance_amount(self, amount: Decimal) -> str:
        return self._format_rub_approx(amount)

    def _listing_display_title(self, *, display_title: str | None, fallback: str) -> str:
        normalized = (display_title or "").strip()
        return normalized or fallback.strip()

    def _sanitize_buyer_display_title(
        self,
        *,
        wb_product_id: int,
        source_title: str,
        brand_name: str | None,
    ) -> str:
        return sanitize_buyer_display_title(
            wb_product_id=wb_product_id,
            source_title=source_title,
            brand_name=brand_name,
        )

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
        percent = (cashback_rub / Decimal(reference_price_rub) * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
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

    async def _parse_ton_mainnet_address(self, *, address: str) -> str:
        normalized_address = address.strip()
        if not normalized_address:
            raise ValueError("Адрес не может быть пустым. Повторите ввод.")
        if ":" not in normalized_address and len(normalized_address) == 48:
            first_char = normalized_address[0]
            if first_char in _TON_FRIENDLY_TESTNET_PREFIXES:
                raise ValueError("Нужен адрес USDT в сети TON mainnet. Testnet-адреса не поддерживаются.")
            if first_char not in _TON_FRIENDLY_MAINNET_PREFIXES:
                raise ValueError("Похоже, адрес введен в неверном формате. Повторите ввод.")
        if self._tonapi_client is None:
            raise TonapiApiError(status_code=None, message="TonAPI client is not ready")
        parsed = await self._tonapi_client.parse_address(account_id=normalized_address)
        return parsed.raw_form

    async def _resolve_payout_wallet_raw_form(self) -> str:
        if self._payout_wallet_raw_form:
            return self._payout_wallet_raw_form
        self._payout_wallet_raw_form = await self._parse_ton_mainnet_address(
            address=self._settings.seller_collateral_shard_address
        )
        return self._payout_wallet_raw_form

    async def _validate_withdrawal_completion_tx(
        self,
        *,
        tx_hash: str,
        payout_address: str,
        amount_usdt: Decimal,
    ) -> str | None:
        normalized_tx_hash = tx_hash.strip()
        if not normalized_tx_hash:
            return "Хэш перевода не может быть пустым."
        if self._tonapi_client is None:
            raise TonapiApiError(status_code=None, message="TonAPI client is not ready")

        payout_wallet_raw = (await self._resolve_payout_wallet_raw_form()).strip().lower()
        destination_raw = (await self._parse_ton_mainnet_address(address=payout_address)).strip().lower()
        expected_amount = amount_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
        before_lt: int | None = None

        for _ in range(self._settings.tonapi_max_pages_per_shard):
            page = await self._tonapi_client.get_jetton_account_history(
                account_id=self._settings.seller_collateral_shard_address,
                jetton_id=self._settings.tonapi_usdt_jetton_master,
                limit=self._settings.tonapi_page_limit,
                before_lt=before_lt,
            )
            if not page.operations:
                break

            for operation in page.operations:
                if operation.transaction_hash != normalized_tx_hash:
                    continue
                source_raw = (operation.source_address or "").strip().lower()
                recipient_raw = (operation.destination_address or "").strip().lower()
                amount_on_chain = operation.amount_usdt.quantize(
                    _USDT_EXACT_QUANT,
                    rounding=ROUND_HALF_UP,
                )

                if source_raw != payout_wallet_raw:
                    return "Транзакция найдена, но отправлена не с настроенного payout-кошелька."
                if recipient_raw != destination_raw:
                    return "Транзакция найдена, но адрес получателя не совпадает с заявкой."
                if amount_on_chain != expected_amount:
                    return "Транзакция найдена, но сумма не совпадает с заявкой."
                return None

            if page.next_from is None:
                break
            before_lt = page.next_from

        return "Транзакция с таким хэшем пока не найдена в истории TON USDT. Повторите попытку позже."

    @staticmethod
    def _screen_text(
        *,
        title: str,
        title_suffix_html: str | None = None,
        cta: str | None = None,
        lines: list[str] | None = None,
        note: str | None = None,
        warning: bool = False,
        separate_blocks: bool = False,
    ) -> str:
        plain_title = html.unescape(title)
        if title.startswith(("🧑‍💼 ", "🛍️ ", "🏪 ", "📦 ", "📋 ", "💳 ", "💰 ", "📘 ")):
            decorated_title = title
        elif plain_title.startswith(("Инструкция", "Про ")):
            decorated_title = f"📘 {title}"
        elif plain_title.startswith("Кабинет продавца"):
            decorated_title = f"🧑‍💼 {title}"
        elif plain_title.startswith("Кабинет покупателя"):
            decorated_title = f"🛍️ {title}"
        elif plain_title.startswith(
            ("Магазины", "Магазин", "Токен WB API", "Создание магазина", "Переименование магазина", "Удаление магазина")
        ):
            decorated_title = f"🏪 {title}"
        elif plain_title.startswith(
            (
                "Объявления",
                "Название объявления",
                "Проверьте объявление",
                "Нужна цена покупателя",
                "Подтвердите изменения",
                "Редактирование объявления",
                "Новое объявление",
                "Удаление объявления",
                "Редактирование отключено",
                "🟢 ",
                "🔴 ",
            )
        ):
            decorated_title = f"📦 {title}"
        elif plain_title.startswith(("Покупки", "Покупка", "Токен-подтверждение", "Токен отзыва", "Отмена покупки")):
            decorated_title = f"📋 {title}"
        elif plain_title.startswith(("Счет на пополнение", "Как перевести USDT")):
            decorated_title = f"💰 {title}"
        elif plain_title.startswith(("Баланс", "Транзакции", "Отмена вывода")):
            decorated_title = f"💳 {title}"
        else:
            decorated_title = title
        title_html = f"{'⚠️ ' if warning else ''}<b>{decorated_title}</b>"
        if title_suffix_html:
            title_html += title_suffix_html
        parts = [title_html]
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
    def _button_label_with_count(label: str, count: int | None) -> str:
        if count is None:
            return label
        normalized_count = max(0, int(count))
        return f"{label} · {normalized_count}"

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

    def _title_ref_suffix(self, value: str | None) -> str | None:
        if not value:
            return None
        return f" · {self._format_copyable_code(value)}"

    @staticmethod
    def _entity_block_heading(label: str) -> str:
        return f"<b>{html.escape(label)}</b>"

    def _entity_block_heading_with_ref(self, *, label: str, ref: str | None = None) -> str:
        heading = self._entity_block_heading(label)
        if not ref:
            return heading
        return f"{heading} · {self._format_copyable_code(ref)}"

    @staticmethod
    def _shop_ref(shop_id: int) -> str:
        return format_shop_ref(shop_id)

    @staticmethod
    def _listing_ref(listing_id: int) -> str:
        return format_listing_ref(listing_id)

    @staticmethod
    def _assignment_ref(assignment_id: int) -> str:
        return format_assignment_ref(assignment_id)

    @staticmethod
    def _parse_assignment_reference(value: str) -> int:
        return parse_assignment_ref(value)

    @staticmethod
    def _withdrawal_ref(withdrawal_request_id: int) -> str:
        return format_withdrawal_ref(withdrawal_request_id)

    @staticmethod
    def _deposit_ref(deposit_intent_id: int) -> str:
        return format_deposit_ref(deposit_intent_id)

    @staticmethod
    def _chain_tx_ref(chain_tx_id: int) -> str:
        return format_chain_tx_ref(chain_tx_id)

    @staticmethod
    def _parse_withdrawal_reference(value: str) -> int:
        return parse_withdrawal_ref(value)

    @staticmethod
    def _parse_deposit_reference(value: str) -> int:
        return parse_deposit_ref(value)

    @staticmethod
    def _parse_chain_tx_reference(value: str) -> int:
        return parse_chain_tx_ref(value)

    def _build_support_link(
        self,
        *,
        role: str,
        topic: str = "generic",
        refs: list[str] | tuple[str, ...] | None = None,
    ) -> str | None:
        support_bot_username = self._settings.support_bot_username
        if not support_bot_username:
            return None
        return build_support_deep_link(
            bot_username=support_bot_username,
            role=role,
            topic=topic,
            refs=refs or (),
        )

    def _build_support_button(
        self,
        *,
        role: str,
        topic: str = "generic",
        refs: list[str] | tuple[str, ...] | None = None,
        text: str = "🆘 Поддержка",
    ) -> InlineKeyboardButton | None:
        support_link = self._build_support_link(role=role, topic=topic, refs=refs)
        if support_link is None:
            return None
        return InlineKeyboardButton(text=text, url=support_link)

    def _knowledge_button(self, *, role: str, topic: str) -> InlineKeyboardButton:
        if role == _ROLE_SELLER:
            mapping = {
                "guide": ("📘 Инструкция", "kb_guide"),
                "shops": ("📘 Про магазины", "kb_shops"),
                "listings": ("📘 Про объявления", "kb_listings"),
                "balance": ("📘 Про баланс и вывод", "kb_balance"),
            }
        elif role == _ROLE_BUYER:
            mapping = {
                "guide": ("📘 Инструкция", "kb_guide"),
                "shops": ("📘 Про магазины", "kb_shops"),
                "purchases": ("📘 Про покупки", "kb_purchases"),
                "balance": ("📘 Про баланс и вывод", "kb_balance"),
            }
        else:
            raise ValueError(f"unsupported knowledge button role: {role}")
        label, action = mapping[topic]
        return InlineKeyboardButton(
            text=label,
            callback_data=build_callback(flow=role, action=action),
        )

    def _build_ton_usdt_wallet_link(
        self,
        *,
        destination_address: str,
        expected_amount_usdt: Decimal,
        text: str | None = None,
    ) -> str:
        normalized_address = destination_address.strip()
        base_units = int(expected_amount_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP) * Decimal("1000000"))
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

    def _build_buyer_listing_token(
        self,
        *,
        task_uuid: str,
        search_phrase: str,
        wb_product_id: int,
        brand_name: str | None,
    ) -> str:
        payload = [
            1,
            task_uuid,
            search_phrase,
            wb_product_id,
            _BUYER_TASK_COMPANION_PRODUCTS,
            (brand_name or "").strip(),
        ]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _build_buyer_review_token(
        self,
        *,
        task_uuid: str,
        wb_product_id: int,
        review_phrases: list[str] | None,
    ) -> str:
        payload: list[Any] = [2, task_uuid, wb_product_id]
        payload.extend(self._normalize_review_phrases(review_phrases)[:2])
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _buyer_task_instruction_text(self, assignment, *, include_title: bool = True) -> str:
        listing_token = self._build_buyer_listing_token(
            task_uuid=str(assignment.task_uuid),
            search_phrase=assignment.search_phrase,
            wb_product_id=assignment.wb_product_id,
            brand_name=getattr(assignment, "wb_brand_name", None),
        )
        reservation_deadline = self._format_datetime_msk(getattr(assignment, "reservation_expires_at", None))
        display_title = self._listing_display_title(
            display_title=getattr(assignment, "display_title", None),
            fallback=assignment.search_phrase,
        )
        lines: list[str] = []
        if include_title:
            lines.append(f"<b>Товар:</b> {html.escape(display_title)}")
        lines.extend(
            [
                (
                    '1. Введите токен в <a href="'
                    f"{_QPILKA_EXTENSION_URL}"
                    '">расширении для браузера Chrome / Яндекс Qpilka</a>:'
                ),
                f"<code>{listing_token}</code>",
                (
                    "2. Выполните шаги, описанные в расширении, и оформите заказ "
                    f"до {reservation_deadline} (по истечении срока бронь отменится)."
                ),
                "3. Отправьте токен-подтверждение сюда.",
            ]
        )
        return "\n".join(lines)

    def _buyer_review_instruction_text(self, assignment, *, include_title: bool = True) -> str:
        review_token = self._build_buyer_review_token(
            task_uuid=str(assignment.task_uuid),
            wb_product_id=assignment.wb_product_id,
            review_phrases=getattr(assignment, "review_phrases", None),
        )
        display_title = self._listing_display_title(
            display_title=getattr(assignment, "display_title", None),
            fallback=assignment.search_phrase,
        )
        lines: list[str] = []
        if include_title:
            lines.append(f"<b>Товар:</b> {html.escape(display_title)}")
        lines.append("<b>Следующий шаг:</b> оставьте отзыв на 5 звезд через Qpilka.")
        selected_phrases = self._normalize_review_phrases(getattr(assignment, "review_phrases", None))
        if selected_phrases:
            lines.append("<b>Фразы для отзыва:</b> " + html.escape(self._format_review_phrases_text(selected_phrases)))
        lines.extend(
            [
                (
                    '1. Введите токен в <a href="'
                    f"{_QPILKA_EXTENSION_URL}"
                    '">расширении для браузера Chrome / Яндекс Qpilka</a>:'
                ),
                f"<code>{review_token}</code>",
                "2. Следуйте подсказкам расширения и отправьте токен-подтверждение отзыва сюда.",
            ]
        )
        return "\n".join(lines)

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
            "rejected": "red",
            "withdraw_pending_admin": "yellow",
            "cancelled": "blue",
        }.get(status, "blue")
        return self._status_badge(label, color=color)

    @staticmethod
    def _humanize_assignment_status(status: str) -> str:
        mapping = {
            "reserved": "Ожидает заказа",
            "order_verified": "Заказан",
            "picked_up_wait_review": "Нужно оставить отзыв",
            "picked_up_wait_unlock": "Выкуплен",
            "withdraw_sent": "Выплачен",
            "expired_2h": "Бронь истекла",
            "buyer_cancelled": "Покупка отменена",
            "wb_invalid": "Не подтвержден",
            "returned_within_14d": "Возвращен",
            "delivery_expired": "Срок выкупа истек",
        }
        return mapping.get(status, status)

    @staticmethod
    def _buyer_visible_assignments(assignments):
        visible_statuses = {
            "reserved",
            "order_verified",
            "picked_up_wait_review",
            "picked_up_wait_unlock",
            "withdraw_sent",
        }
        return [item for item in assignments if item.status in visible_statuses]

    @staticmethod
    def _buyer_shop_title(assignment) -> str:
        title = str(getattr(assignment, "shop_title", "") or "").strip()
        if title:
            return title
        return str(getattr(assignment, "shop_slug", "") or "").strip()

    @staticmethod
    def _buyer_shop_activity_badge(active_listings_count: int) -> str:
        return "🟢" if active_listings_count > 0 else "🔴"

    @staticmethod
    def _buyer_dashboard_status_bucket(status: str) -> str | None:
        if status == "reserved":
            return "awaiting_order"
        if status in {"order_verified", "picked_up_wait_review", "wb_invalid", "delivery_expired"}:
            return "ordered"
        if status in {
            "picked_up_wait_unlock",
            "withdraw_sent",
            "returned_within_14d",
        }:
            return "picked_up"
        return None

    def _buyer_purchase_status_badge(self, status: str) -> str:
        bucket = self._buyer_dashboard_status_bucket(status)
        if bucket == "awaiting_order":
            color = "red"
        elif bucket == "ordered":
            color = "yellow"
        elif bucket == "picked_up":
            color = "green"
        else:
            color = "blue"
        return self._status_badge(self._humanize_assignment_status(status), color=color)

    @staticmethod
    def _humanize_withdraw_status(status: str) -> str:
        mapping = {
            "withdraw_pending_admin": "На проверке",
            "rejected": "Отклонено",
            "cancelled": "Отменено",
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
            raise ListingValidationError("Токен магазина невалиден. Обновите токен WB API.") from exc
        try:
            return decrypt_token(ciphertext, self._settings.token_cipher_key)
        except Exception as exc:
            raise ListingValidationError("Не удалось прочитать токен магазина. Сохраните его заново.") from exc

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
        keyboard_rows: list[list[InlineKeyboardButton]] = [
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
        ]
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к объявлениям",
                    callback_data=build_callback(
                        flow=_ROLE_SELLER,
                        action="listings",
                        entity_id=str(list_page),
                    ),
                )
            ]
        )
        keyboard_rows.append([self._knowledge_button(role=_ROLE_SELLER, topic="listings")])
        return InlineKeyboardMarkup(keyboard_rows)

    @staticmethod
    def _listing_has_sufficient_collateral(
        *,
        collateral_view,
        seller_available_usdt: Decimal = Decimal("0.000000"),
        listing_status: str | None = None,
    ) -> bool:
        if collateral_view is None:
            return True
        status = listing_status or getattr(collateral_view, "status", None)
        effective_collateral = collateral_view.collateral_locked_usdt
        if status == "draft":
            effective_collateral += seller_available_usdt
        return effective_collateral >= collateral_view.collateral_required_usdt

    def _format_listing_collateral_line(
        self,
        *,
        collateral_view,
        seller_available_usdt: Decimal = Decimal("0.000000"),
    ) -> str:
        if collateral_view is None:
            return "—"
        required_text = self._format_usdt_with_rub(collateral_view.collateral_required_usdt)
        if self._listing_has_sufficient_collateral(
            collateral_view=collateral_view,
            seller_available_usdt=seller_available_usdt,
        ):
            return f"🟢 {required_text}"
        return f"🔴 {self._format_usdt(collateral_view.collateral_required_usdt)} (недостаточно средств)"

    def _listing_detail_note(
        self,
        *,
        listing,
        collateral_view,
        seller_available_usdt: Decimal = Decimal("0.000000"),
    ) -> str:
        if listing.status == "active":
            return "Объявление активно. При необходимости поставьте его на паузу или поделитесь ссылкой на магазин."
        if not self._listing_has_sufficient_collateral(
            collateral_view=collateral_view,
            seller_available_usdt=seller_available_usdt,
            listing_status=listing.status,
        ):
            return "Для активации пополните баланс продавца, затем вернитесь к карточке объявления."
        return "Проверьте параметры и активируйте объявление, когда будете готовы."

    def _seller_listing_detail_html(
        self,
        *,
        listing,
        collateral_view,
        seller_available_usdt: Decimal = Decimal("0.000000"),
        shop_link: str | None = None,
        notice: str | None = None,
    ) -> str:
        display_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        planned = collateral_view.slot_count if collateral_view is not None else listing.slot_count
        in_progress = collateral_view.in_progress_assignments_count if collateral_view is not None else 0
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
                f"<b>План покупок / В процессе:</b> {planned} / {in_progress}",
            ]
        )
        if shop_link:
            lines.append(f"<b>Ссылка на магазин:</b>\n{html.escape(shop_link)}")
        collateral_line = self._format_listing_collateral_line(
            collateral_view=collateral_view,
            seller_available_usdt=seller_available_usdt,
        )
        lines.extend(
            [
                f"<b>Обеспечение:</b> {collateral_line}",
                (f"<b>Статус:</b> {self._listing_activity_badge(is_active=is_active)}"),
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
            )
            .replace("<b>", "")
            .replace("</b>", ""),
            (
                "Фразы для отзыва: "
                + html.escape(self._format_review_phrases_text(getattr(listing, "review_phrases", [])))
            ),
            f"Размеры: {html.escape(self._format_sizes_text(listing.wb_tech_sizes))}",
        ]
        lines.append("\n<b>Параметры</b>\n<blockquote expandable>" + "\n".join(parameters_lines) + "</blockquote>")
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
            title_suffix_html=self._title_ref_suffix(self._listing_ref(listing.listing_id)),
            cta="Проверьте объявление и выберите следующее действие ниже.",
            lines=lines,
            note=self._listing_detail_note(
                listing=listing,
                collateral_view=collateral_view,
                seller_available_usdt=seller_available_usdt,
            ),
        )

    def _buyer_listing_detail_html(self, *, listing, notice: str | None = None) -> str:
        display_title = self._listing_display_title(
            display_title=listing.display_title,
            fallback=listing.search_phrase,
        )
        lines: list[str] = []
        if notice:
            lines.append(html.escape(notice))
        cashback_text = self._format_buyer_cashback_with_percent(
            reward_usdt=listing.reward_usdt,
            reference_price_rub=listing.reference_price_rub,
        )
        lines.extend(
            [
                f"<b>Предмет:</b> {html.escape(listing.wb_subject_name or '—')}",
                self._format_listing_price_line(
                    label="Цена",
                    price_rub=listing.reference_price_rub,
                    source=None,
                ),
                f"<b>Кэшбэк:</b> {html.escape(cashback_text)}",
                f"<b>Поисковая фраза:</b> &quot;{html.escape(listing.search_phrase)}&quot;",
            ]
        )
        if self._should_show_buyer_sizes(listing.wb_tech_sizes):
            lines.append(f"<b>Размеры:</b> {html.escape(self._format_sizes_text(listing.wb_tech_sizes))}")
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
            title=f"📦 {display_title}",
            title_suffix_html=self._title_ref_suffix(self._listing_ref(listing.listing_id)),
            cta="Проверьте товар и выберите следующее действие ниже.",
            lines=lines,
            separate_blocks=True,
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
        return f"<b>{html.escape(label)}:</b> {self._format_price_rub(price_rub)}{html.escape(suffix)}"

    @staticmethod
    def _normalize_review_phrases(review_phrases: list[str] | None) -> list[str]:
        normalized: list[str] = []
        for phrase in review_phrases or []:
            cleaned = str(phrase).strip()
            if cleaned:
                normalized.append(cleaned)
        return normalized

    def _format_review_phrases_text(self, review_phrases: list[str] | None) -> str:
        normalized = self._normalize_review_phrases(review_phrases)
        if not normalized:
            return "не заданы"
        return "; ".join(normalized)

    @staticmethod
    def _normalize_sizes(sizes: list[str] | None) -> list[str]:
        if not sizes:
            return []
        normalized: list[str] = []
        for size in sizes:
            cleaned = str(size).strip()
            if cleaned:
                normalized.append(cleaned)
        return normalized

    def _should_show_buyer_sizes(self, sizes: list[str] | None) -> bool:
        return self._normalize_sizes(sizes) != ["0"]

    def _format_sizes_text(self, sizes: list[str] | None) -> str:
        normalized = self._normalize_sizes(sizes)
        if not normalized:
            return "—"
        return ", ".join(normalized)

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
        return f"<b>{html.escape(title)}</b>\n<blockquote expandable>{html.escape(normalized)}</blockquote>"

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

    def _seller_menu_markup(
        self,
        *,
        listings_count: int | None = None,
        shops_count: int | None = None,
    ) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(
                    text=self._button_label_with_count("📦 Объявления", listings_count),
                    callback_data=build_callback(flow=_ROLE_SELLER, action="listings"),
                ),
                InlineKeyboardButton(
                    text=self._button_label_with_count("🏬 Магазины", shops_count),
                    callback_data=build_callback(flow=_ROLE_SELLER, action="shops"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Баланс",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="balance"),
                ),
            ],
            [self._knowledge_button(role=_ROLE_SELLER, topic="guide")],
        ]
        support_button = self._build_support_button(role=_ROLE_SELLER)
        if support_button is not None:
            keyboard.append([support_button])
        return InlineKeyboardMarkup(keyboard)

    def _seller_balance_menu_markup(
        self,
        *,
        can_withdraw_available: bool = False,
        active_request_id: int | None = None,
    ) -> InlineKeyboardMarkup:
        keyboard: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="➕ Пополнить",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="topup_prompt"),
                )
            ]
        ]
        if active_request_id is not None:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text="🚫 Отменить заявку",
                        callback_data=build_callback(
                            flow=_ROLE_SELLER,
                            action="withdraw_cancel_prompt",
                            entity_id=str(active_request_id),
                        ),
                    )
                ]
            )
        elif can_withdraw_available:
            keyboard.extend(
                [
                    [
                        InlineKeyboardButton(
                            text="💸 Вывести все доступное",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="withdraw_full",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="✍️ Указать сумму вручную",
                            callback_data=build_callback(
                                flow=_ROLE_SELLER,
                                action="withdraw_prompt_amount",
                            ),
                        )
                    ],
                ]
            )
        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🧾 Транзакции",
                        callback_data=build_callback(flow=_ROLE_SELLER, action="topup_history"),
                    )
                ],
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=build_callback(flow=_ROLE_SELLER, action="menu"),
                )
            ]
        )
        keyboard.append([self._knowledge_button(role=_ROLE_SELLER, topic="balance")])
        return InlineKeyboardMarkup(keyboard)

    def _buyer_menu_markup(
        self,
        *,
        shops_count: int | None = None,
        purchases_count: int | None = None,
    ) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(
                    text=self._button_label_with_count("🏪 Магазины", shops_count),
                    callback_data=build_callback(
                        flow=_ROLE_BUYER,
                        action="shops",
                    ),
                ),
                InlineKeyboardButton(
                    text=self._button_label_with_count("📋 Покупки", purchases_count),
                    callback_data=build_callback(flow=_ROLE_BUYER, action="assignments"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Баланс и вывод",
                    callback_data=build_callback(flow=_ROLE_BUYER, action="balance"),
                ),
            ],
            [self._knowledge_button(role=_ROLE_BUYER, topic="guide")],
        ]
        return InlineKeyboardMarkup(keyboard)

    def _buyer_review_followup_markup(self, *, assignment_id: int) -> InlineKeyboardMarkup:
        keyboard: list[list[InlineKeyboardButton]] = []
        support_button = self._build_support_button(
            role=_ROLE_BUYER,
            topic="review",
            refs=[self._assignment_ref(assignment_id)],
            text="🆘 Поддержка",
        )
        if support_button is not None:
            keyboard.append([support_button])
        keyboard.extend(self._buyer_menu_markup().inline_keyboard)
        return InlineKeyboardMarkup(keyboard)

    def _admin_menu_markup(
        self,
        *,
        pending_withdrawals_count: int | None = None,
        deposit_exceptions_count: int | None = None,
        exceptions_count: int | None = None,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=self._button_label_with_count("💸 Выводы", pending_withdrawals_count),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawals_section",
                        ),
                    ),
                    InlineKeyboardButton(
                        text=self._button_label_with_count("🏦 Депозиты", deposit_exceptions_count),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposits_section",
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=self._button_label_with_count("⚠️ Исключения", exceptions_count),
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
            raise RuntimeError(f"runtime schema compatibility check failed; missing columns: {missing_list}")

        self._logger.info(
            "telegram_runtime_schema_compatibility_ok",
            required_tables=len(_RUNTIME_REQUIRED_SCHEMA_COLUMNS),
            required_columns=sum(len(columns) for columns in _RUNTIME_REQUIRED_SCHEMA_COLUMNS.values()),
        )

    def _build_webhook_url(self) -> str:
        if not self._settings.webhook_base_url:
            raise ValueError(
                "WEBHOOK_BASE_URL is required for webhook runtime (example: https://158.160.187.114:8443)."
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
