from __future__ import annotations

import asyncio
import html
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from psycopg.rows import dict_row

from libs.config.settings import BotApiSettings
from libs.db.pool import DatabasePool
from libs.domain.buyer import BuyerService
from libs.domain.deposit_intents import DepositIntentService
from libs.domain.errors import (
    DomainError,
    InsufficientFundsError,
    InvalidStateError,
    ListingValidationError,
    NotFoundError,
)
from libs.domain.fx_rates import FxRateService
from libs.domain.ledger import FinanceService
from libs.domain.listing_creation import sanitize_buyer_display_title
from libs.domain.notifications import NotificationService
from libs.domain.public_refs import (
    build_support_deep_link,
    format_assignment_ref,
    format_deposit_ref,
    format_listing_ref,
    format_shop_ref,
    format_withdrawal_ref,
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
from libs.integrations.yandex_monitoring import YandexMonitoringMetricClient, YandexMonitoringMetricRecorder
from libs.logging.setup import EventLogger, get_logger
from libs.security.token_cipher import decrypt_token
from services.bot_api.admin_exceptions_flow import (
    AdminExceptionsAdapter,
    AdminExceptionsFlow,
)
from services.bot_api.buyer_handlers import BuyerCommandProcessor
from services.bot_api.buyer_marketplace_flow import (
    BuyerMarketplaceAdapter,
    BuyerMarketplaceFlow,
    BuyerMarketplaceFlowConfig,
    classify_buyer_token_text,
)
from services.bot_api.callback_data import (
    CALLBACK_VERSION,
    CallbackPayload,
    build_callback,
    parse_callback,
)
from services.bot_api.deep_links import (
    build_listing_deep_link,
    parse_start_payload,
)
from services.bot_api.presentation import (
    button_label_with_count,
    entity_block_heading_with_ref,
    format_cashback_with_percent,
    format_datetime_msk,
    format_usdt_value,
    format_usdt_with_rub,
    humanize_withdraw_status,
    resolve_numbered_page,
    screen_text,
    status_badge,
    title_ref_suffix,
    withdraw_status_badge,
)
from services.bot_api.seller_handlers import SellerCommandProcessor
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.seller_marketplace_flow import SellerMarketplaceFlow, SellerMarketplaceFlowConfig
from services.bot_api.telegram_notifications import render_telegram_notification
from services.bot_api.telegram_proxy_request import build_telegram_proxy_request
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
    SetUserData,
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
        InputFile,
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
        "python-telegram-bot is required for the Telegram runtime. "
        "Install dependencies from pyproject/requirements before running the marketplace bot."
    ) from exc


_ROLE_SELLER = "seller"
_ROLE_BUYER = "buyer"
_ROLE_ADMIN = "admin"

_ACTIVE_ROLE_KEY = "active_role"
_LAST_BUYER_SHOP_SLUG_KEY = "last_buyer_shop_slug"
_PROMPT_STATE_KEY = "prompt_state"
_SELLER_LISTINGS_PAGE_KEY = "seller_listings_page"
_USDT_EXACT_QUANT = Decimal("0.000001")
_RUB_QUANT = Decimal("1")
_LISTING_COLLATERAL_FEE_MULTIPLIER = Decimal("1.01")
_TON_FRIENDLY_MAINNET_PREFIXES = frozenset({"E", "U"})
_TON_FRIENDLY_TESTNET_PREFIXES = frozenset({"k", "0"})
_PHOTO_DOWNLOAD_TIMEOUT_SECONDS = 10
_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_JPEG_QUALITY = 88
_PHOTO_HTTP_HEADERS = {
    "User-Agent": "qpi-bot/1.0",
    "Accept": "image/webp,image/jpeg,image/png,*/*;q=0.8",
}
_SUPPORTED_UPLOAD_IMAGE_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_SUPPORTED_BINARY_IMAGE_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
_TRUSTED_WB_PHOTO_ROOT_HOSTS = frozenset({"wbbasket.ru", "wbcontent.net"})

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
TELEGRAM_UPDATE_RECEIVED_METRIC = "qpi.telegram.update.received"
TELEGRAM_UPDATE_DELIVERY_LAG_METRIC = "qpi.telegram.update.delivery_lag_seconds"
TELEGRAM_CALLBACK_ANSWER_FAILURE_METRIC = "qpi.telegram.callback.answer_failure"


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None


@dataclass(frozen=True)
class _DownloadedPhoto:
    data: bytes
    content_type: str | None
    final_url: str


class _PhotoDownloadError(RuntimeError):
    pass


class _TrustedPhotoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not _is_wb_photo_url(newurl):
            raise _PhotoDownloadError("redirect target is not a trusted WB photo host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_PHOTO_URL_OPENER = urllib.request.build_opener(_TrustedPhotoRedirectHandler)


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


class _RuntimeBuyerMarketplaceAdapter(BuyerMarketplaceAdapter):
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def get_buyer_balance_snapshot(self, *, buyer_user_id: int) -> Any:
        return await self._runtime._finance_service.get_buyer_balance_snapshot(buyer_user_id=buyer_user_id)

    async def get_active_buyer_withdrawal_request(self, *, buyer_user_id: int) -> Any | None:
        return await self._runtime._finance_service.get_active_buyer_withdrawal_request(buyer_user_id=buyer_user_id)

    async def count_buyer_withdrawal_history(self, *, buyer_user_id: int) -> int:
        return await self._runtime._finance_service.count_buyer_withdrawal_history(buyer_user_id=buyer_user_id)

    async def list_buyer_withdrawal_history(self, *, buyer_user_id: int, limit: int, offset: int) -> list[Any]:
        return await self._runtime._finance_service.list_buyer_withdrawal_history(
            buyer_user_id=buyer_user_id,
            limit=limit,
            offset=offset,
        )

    async def list_buyer_assignments(self, *, buyer_user_id: int) -> list[Any]:
        return await self._runtime._buyer_service.list_buyer_assignments(buyer_user_id=buyer_user_id)

    async def list_saved_shops(self, *, buyer_user_id: int, limit: int = 20) -> list[Any]:
        return await self._runtime._buyer_service.list_saved_shops(buyer_user_id=buyer_user_id, limit=limit)

    async def resolve_shop_by_slug(self, *, slug: str) -> Any:
        return await self._runtime._buyer_service.resolve_shop_by_slug(slug=slug)

    async def list_active_listings_by_shop_slug(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
    ) -> list[Any]:
        return await self._runtime._buyer_service.list_active_listings_by_shop_slug(
            slug=slug,
            buyer_user_id=buyer_user_id,
        )

    async def resolve_active_listing_deep_link(
        self,
        *,
        listing_id: int,
        buyer_user_id: int | None = None,
    ) -> Any:
        return await self._runtime._buyer_service.resolve_active_listing_deep_link(
            listing_id=listing_id,
            buyer_user_id=buyer_user_id,
        )

    async def touch_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> None:
        await self._runtime._buyer_service.touch_saved_shop(buyer_user_id=buyer_user_id, shop_id=shop_id)

    async def resolve_saved_shop_for_buyer(self, *, buyer_user_id: int, shop_id: int) -> Any:
        return await self._runtime._buyer_service.resolve_saved_shop_for_buyer(
            buyer_user_id=buyer_user_id,
            shop_id=shop_id,
        )

    async def remove_saved_shop(self, *, buyer_user_id: int, shop_id: int) -> Any:
        return await self._runtime._buyer_service.remove_saved_shop(buyer_user_id=buyer_user_id, shop_id=shop_id)

    async def reserve_listing_slot(
        self,
        *,
        buyer_user_id: int,
        listing_id: int,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._buyer_service.reserve_listing_slot(
            buyer_user_id=buyer_user_id,
            listing_id=listing_id,
            idempotency_key=idempotency_key,
        )

    async def submit_purchase_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any:
        return await self._runtime._buyer_service.submit_purchase_payload(
            buyer_user_id=buyer_user_id,
            assignment_id=assignment_id,
            payload_base64=payload_base64,
        )

    async def submit_purchase_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> Any:
        return await self._runtime._buyer_service.submit_purchase_payload_by_task_uuid(
            buyer_user_id=buyer_user_id,
            payload_base64=payload_base64,
        )

    async def submit_review_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> Any:
        return await self._runtime._buyer_service.submit_review_payload(
            buyer_user_id=buyer_user_id,
            assignment_id=assignment_id,
            payload_base64=payload_base64,
        )

    async def submit_review_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> Any:
        return await self._runtime._buyer_service.submit_review_payload_by_task_uuid(
            buyer_user_id=buyer_user_id,
            payload_base64=payload_base64,
        )

    async def cancel_assignment_by_buyer(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._buyer_service.cancel_assignment_by_buyer(
            buyer_user_id=buyer_user_id,
            assignment_id=assignment_id,
            idempotency_key=idempotency_key,
        )


class _RuntimeAdminExceptionsAdapter(AdminExceptionsAdapter):
    def __init__(self, runtime: TelegramWebhookRuntime) -> None:
        self._runtime = runtime

    async def list_pending_review_confirmations(self, *, limit: int = 1000) -> list[Any]:
        return await self._runtime._buyer_service.list_admin_pending_review_confirmations(limit=limit)

    async def list_admin_review_txs(self, *, limit: int = 1000) -> list[Any]:
        return await self._runtime._deposit_service.list_admin_review_txs(limit=limit)

    async def list_admin_expired_intents(self, *, limit: int = 1000) -> list[Any]:
        return await self._runtime._deposit_service.list_admin_expired_intents(limit=limit)

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> Any:
        return await self._runtime._buyer_service.admin_verify_review_payload(
            admin_user_id=admin_user_id,
            assignment_id=assignment_id,
            payload_base64=payload_base64,
            idempotency_key=idempotency_key,
        )

    async def credit_intent_from_chain_tx(
        self,
        *,
        deposit_intent_id: int,
        chain_tx_id: int,
        idempotency_key: str,
        admin_user_id: int,
        allow_expired: bool,
    ) -> Any:
        return await self._runtime._deposit_service.credit_intent_from_chain_tx(
            deposit_intent_id=deposit_intent_id,
            chain_tx_id=chain_tx_id,
            idempotency_key=idempotency_key,
            admin_user_id=admin_user_id,
            allow_expired=allow_expired,
        )

    async def cancel_deposit_intent(
        self,
        *,
        deposit_intent_id: int,
        admin_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> bool:
        return await self._runtime._deposit_service.cancel_deposit_intent(
            deposit_intent_id=deposit_intent_id,
            admin_user_id=admin_user_id,
            reason=reason,
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


def _http_url_hostname(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(value)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    return host.rstrip(".").lower()


def _is_http_url(value: str) -> bool:
    return _http_url_hostname(value) is not None


def _is_webp_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.path.lower().endswith(".webp")


def _is_wb_photo_url(value: str) -> bool:
    host = _http_url_hostname(value)
    if host is None:
        return False
    if host in _TRUSTED_WB_PHOTO_ROOT_HOSTS:
        return True
    if any(host.endswith(f".{root_host}") for root_host in _TRUSTED_WB_PHOTO_ROOT_HOSTS):
        return True
    return host.startswith("basket-") and host.endswith(".wb.ru")


def _is_supported_photo_content_type(content_type: str | None) -> bool:
    return (
        not content_type
        or content_type in _SUPPORTED_UPLOAD_IMAGE_TYPES
        or content_type in _SUPPORTED_BINARY_IMAGE_TYPES
    )


def _download_photo_from_url(photo_url: str) -> _DownloadedPhoto:
    if not _is_wb_photo_url(photo_url):
        raise _PhotoDownloadError("photo URL is not a trusted WB photo host")

    request = urllib.request.Request(photo_url, headers=_PHOTO_HTTP_HEADERS)
    try:
        with _PHOTO_URL_OPENER.open(request, timeout=_PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise _PhotoDownloadError(f"HTTP {status}")

            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not _is_supported_photo_content_type(content_type):
                raise _PhotoDownloadError(f"unsupported content type: {content_type}")

            content_length_text = response.headers.get("Content-Length")
            if content_length_text:
                try:
                    content_length = int(content_length_text)
                except ValueError:
                    content_length = None
                if content_length is not None and content_length > _PHOTO_MAX_BYTES:
                    raise _PhotoDownloadError(f"photo too large: {content_length} bytes")

            data = response.read(_PHOTO_MAX_BYTES + 1)
            if len(data) > _PHOTO_MAX_BYTES:
                raise _PhotoDownloadError(f"photo exceeds {_PHOTO_MAX_BYTES} bytes")
            if not data:
                raise _PhotoDownloadError("photo response is empty")
            final_url = response.geturl()
            if not _is_wb_photo_url(final_url):
                raise _PhotoDownloadError("final photo URL is not a trusted WB photo host")
            return _DownloadedPhoto(data=data, content_type=content_type or None, final_url=final_url)
    except _PhotoDownloadError:
        raise
    except urllib.error.URLError as exc:
        raise _PhotoDownloadError(str(exc)) from exc
    except TimeoutError as exc:
        raise _PhotoDownloadError("timed out") from exc


def _photo_upload_filename(*, photo_url: str, content_type: str | None) -> str:
    if content_type in {"image/jpeg", "image/jpg"}:
        return "listing.jpg"
    if content_type == "image/png":
        return "listing.png"
    if content_type == "image/webp" or _is_webp_url(photo_url):
        return "listing.webp"
    return "listing"


def _convert_image_bytes_to_jpeg(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                alpha_source = image.convert("RGBA")
                background = Image.new("RGBA", alpha_source.size, (255, 255, 255, 255))
                image = Image.alpha_composite(background, alpha_source).convert("RGB")
            elif image.mode != "RGB":
                image = image.convert("RGB")

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=_PHOTO_JPEG_QUALITY, optimize=True)
    except UnidentifiedImageError as exc:
        raise ValueError("photo bytes are not a supported image") from exc

    jpeg_data = output.getvalue()
    if len(jpeg_data) > _PHOTO_MAX_BYTES:
        raise ValueError(f"converted JPEG exceeds {_PHOTO_MAX_BYTES} bytes")
    return jpeg_data


class TelegramWebhookRuntime:
    """Real Telegram runtime with button-first role shell."""

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
        self._seller_listing_creation_flow_rate: Decimal | None = None
        self._seller_marketplace_flow_cache: SellerMarketplaceFlow | None = None
        self._seller_marketplace_flow_rate: Decimal | None = None
        self._seller_withdrawal_creation_flow_cache: WithdrawalRequestCreationFlow | None = None
        self._buyer_withdrawal_creation_flow_cache: WithdrawalRequestCreationFlow | None = None
        self._buyer_marketplace_flow_cache: BuyerMarketplaceFlow | None = None
        self._buyer_marketplace_flow_rate: Decimal | None = None
        self._admin_exceptions_flow_cache: AdminExceptionsFlow | None = None
        self._wb_ping_client: WbPingClient | None = None
        self._wb_public_client: WbPublicCatalogClient | None = None
        self._tonapi_client: TonapiClient | None = None
        self._payout_wallet_raw_form: str | None = None
        self._display_rub_per_usdt = settings.display_rub_per_usdt
        self._notification_dispatch_task: asyncio.Task[None] | None = None
        self._monitoring_recorder = (
            YandexMonitoringMetricRecorder(
                client=YandexMonitoringMetricClient(folder_id=settings.yc_folder_id),
                logger=self._logger,
            )
            if settings.yc_folder_id
            else None
        )

    def run(self) -> None:
        update_mode = self._settings.telegram_update_mode
        webhook_url = self._build_webhook_url() if update_mode == "webhook" else None
        tls_enabled = bool(
            self._settings.webhook_tls_cert_path and self._settings.webhook_tls_key_path,
        )
        self._logger.info(
            "telegram_runtime_starting",
            update_mode=update_mode,
            webhook_url=webhook_url,
            listen_host=self._settings.webhook_listen_host,
            listen_port=self._settings.webhook_listen_port,
            webhook_path=self._settings.webhook_path,
            webhook_tls_enabled=tls_enabled if update_mode == "webhook" else False,
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
            if update_mode == "polling":
                application.run_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
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
                "TELEGRAM_BOT_TOKEN is required for Telegram runtime. "
                "Use --seller-command/--buyer-command for local command adapter mode."
            )
        builder = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        if self._settings.telegram_api_proxy_urls:
            self._logger.info(
                "telegram_proxy_redundancy_enabled",
                proxies_count=len(self._settings.telegram_api_proxy_urls),
                monitoring_enabled=bool(self._settings.yc_folder_id),
            )
            builder = builder.request(
                build_telegram_proxy_request(
                    self._settings.telegram_api_proxy_urls,
                    folder_id=self._settings.yc_folder_id,
                    logger=self._logger,
                )
            )
        application = builder.build()
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
            self._seller_workflow_service = seller_workflow_service
            self._seller_listing_creation_flow = SellerListingCreationFlow(
                seller_service=self._seller_service,
                seller_workflow=seller_workflow_service,
                display_rub_per_usdt=self._settings.display_rub_per_usdt,
                fx_rate_service=self._fx_rate_service,
                fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
                listing_deep_link_builder=self._build_listing_deep_link,
            )
            self._seller_listing_creation_flow_rate = self._settings.display_rub_per_usdt
            self._seller_processor = SellerCommandProcessor(
                seller_service=self._seller_service,
                seller_workflow_service=seller_workflow_service,
                wb_ping_client=wb_ping_client,
                token_cipher_key=self._settings.token_cipher_key,
                bot_username=self._settings.telegram_bot_username,
                display_rub_per_usdt=self._settings.display_rub_per_usdt,
                fx_rate_service=self._fx_rate_service,
                fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
                listing_deep_link_builder=self._build_listing_deep_link,
                listing_creation_flow=self._seller_listing_creation_flow,
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
                "telegram_bot_identity",
                update_mode=self._settings.telegram_update_mode,
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
            if self._settings.telegram_update_mode == "webhook" and self._settings.webhook_set_enabled:
                await self._ensure_webhook_registration(application=application)
            elif self._settings.telegram_update_mode == "polling":
                await self._disable_webhook_registration(application=application)
            self._notification_dispatch_task = asyncio.create_task(
                self._notification_dispatch_loop(bot=application.bot)
            )
            self._ready = True
            self._logger.info("telegram_runtime_ready", update_mode=self._settings.telegram_update_mode)
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._logger.exception(
                "telegram_runtime_init_failed",
                update_mode=self._settings.telegram_update_mode,
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
        self._logger.info("telegram_runtime_stopped", update_mode=self._settings.telegram_update_mode)

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

    async def _disable_webhook_registration(self, *, application: Application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=False)
        webhook_info = await application.bot.get_webhook_info()
        self._logger.info(
            "telegram_webhook_disabled_for_polling",
            webhook_url=webhook_info.url,
            pending_update_count=webhook_info.pending_update_count,
        )

    async def _run_update_handler(
        self,
        update: Update,
        *,
        handler: str,
        callback: Callable[[], Any],
    ) -> None:
        observed_at = datetime.now(UTC)
        try:
            await callback()
        except Exception:
            self._record_update_metrics(update, handler=handler, outcome="failure", observed_at=observed_at)
            raise
        self._record_update_metrics(update, handler=handler, outcome="success", observed_at=observed_at)

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_update_handler(
            update,
            handler="start",
            callback=lambda: self._handle_start_impl(update, context),
        )

    async def _handle_start_impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        start_payload = parse_start_payload(start_args)
        if start_payload is not None:
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
            if start_payload.kind == "shop":
                await self._send_buyer_shop_catalog(
                    update.message,
                    context=context,
                    slug=str(start_payload.value),
                    buyer_user_id=buyer.user_id,
                )
                return
            if start_payload.kind == "listing":
                await self._send_buyer_listing_deep_link(
                    update.message,
                    context=context,
                    listing_id=int(start_payload.value),
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
        await self._run_update_handler(
            update,
            handler="command",
            callback=lambda: self._handle_command_message_impl(update, context),
        )

    async def _handle_command_message_impl(
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
        await self._run_update_handler(
            update,
            handler="text",
            callback=lambda: self._handle_text_impl(update, context),
        )

    async def _handle_text_impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                reply_markup=self._flow_buttons_markup(self._seller_marketplace_flow().menu_buttons()),
            )
            return
        if active_role == _ROLE_BUYER:
            token_kind = classify_buyer_token_text(text)
            if token_kind is not None:
                buyer = await self._buyer_service.bootstrap_buyer(
                    telegram_id=identity.telegram_id,
                    username=identity.username,
                )
                if token_kind == "purchase":
                    result = await self._buyer_marketplace_flow().submit_direct_purchase_payload(
                        text=text,
                        buyer_user_id=buyer.user_id,
                        update_id=update.update_id,
                    )
                else:
                    result = await self._buyer_marketplace_flow().submit_direct_review_payload(
                        text=text,
                        buyer_user_id=buyer.user_id,
                        update_id=update.update_id,
                    )
                await self._apply_transport_effects(
                    context=context,
                    query_message=None,
                    message=update.message,
                    default_role=_ROLE_BUYER,
                    result=result,
                )
                return
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
        await self._run_update_handler(
            update,
            handler="callback",
            callback=lambda: self._handle_callback_impl(update, context),
        )

    async def _handle_callback_impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        raw_payload = query.data or ""
        try:
            payload = parse_callback(raw_payload)
        except ValueError:
            await query.answer("Кнопка устарела", show_alert=True)
            return

        identity = _identity_from_callback(update)
        self._logger.info(
            "telegram_callback_received",
            telegram_update_id=update.update_id,
            flow=payload.flow,
            action=payload.action,
            entity_id=payload.entity_id,
            telegram_id=identity.telegram_id if identity else None,
        )
        try:
            await query.answer()
        except Exception as exc:
            self._logger.warning(
                "telegram_callback_answer_failed",
                telegram_update_id=update.update_id,
                flow=payload.flow,
                action=payload.action,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
            self._record_metric(
                TELEGRAM_CALLBACK_ANSWER_FAILURE_METRIC,
                {
                    "flow": payload.flow,
                    "action": payload.action,
                    "error_type": type(exc).__name__,
                },
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
        if identity is None:
            return

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
            await self._refresh_display_rub_per_usdt()
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().render_dashboard(seller_user_id=seller.user_id),
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
                context=context,
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

        def entity_id_int() -> int | None:
            if not payload.entity_id:
                return None
            try:
                return int(payload.entity_id)
            except ValueError:
                return None

        async def apply(result: FlowResult) -> None:
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_SELLER,
                result=result,
            )

        if action == "menu":
            self._clear_prompt(context)
            await self._refresh_display_rub_per_usdt()
            await apply(await self._seller_marketplace_flow().render_dashboard(seller_user_id=seller.user_id))
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
            await apply(self._seller_marketplace_flow().start_shop_create_token_prompt(seller_user_id=seller.user_id))
            return
        if action == "shops":
            await apply(await self._seller_marketplace_flow().render_shops(seller_user_id=seller.user_id))
            return
        if action == "kb_guide":
            await apply(self._seller_marketplace_flow().render_knowledge_screen(topic="guide"))
            return
        if action == "kb_shops":
            await apply(self._seller_marketplace_flow().render_knowledge_screen(topic="shops"))
            return
        if action == "kb_listings":
            await apply(self._seller_marketplace_flow().render_knowledge_screen(topic="listings"))
            return
        if action == "kb_balance":
            await apply(self._seller_marketplace_flow().render_knowledge_screen(topic="balance"))
            return
        if action == "shop_open":
            shop_id = entity_id_int()
            if shop_id is None:
                await self._replace_message(query_message, "Не удалось открыть магазин. Нажмите кнопку еще раз.")
                return
            await apply(
                await self._seller_marketplace_flow().render_shop_details(
                    seller_user_id=seller.user_id,
                    shop_id=shop_id,
                )
            )
            return
        if action == "shop_delete_preview":
            await apply(
                await self._seller_marketplace_flow().render_shop_delete_preview(
                    seller_user_id=seller.user_id,
                    shop_id=entity_id_int(),
                )
            )
            return
        if action == "shop_delete_confirm":
            await apply(
                await self._seller_marketplace_flow().execute_shop_delete(
                    seller_user_id=seller.user_id,
                    shop_id=entity_id_int(),
                )
            )
            return
        if action == "shop_rename_prompt":
            await apply(
                await self._seller_marketplace_flow().start_shop_rename_prompt(
                    seller_user_id=seller.user_id,
                    shop_id=entity_id_int(),
                )
            )
            return
        if action == "shop_token_prompt":
            await apply(
                await self._seller_marketplace_flow().start_shop_token_prompt(
                    seller_user_id=seller.user_id,
                    shop_id=entity_id_int(),
                )
            )
            return
        if action == "listings":
            requested_page = self._coerce_page_number(payload.entity_id)
            await apply(
                await self._seller_marketplace_flow().render_listings(
                    seller_user_id=seller.user_id,
                    page=requested_page,
                )
            )
            return
        if action == "listing_create_pick_shop":
            await apply(
                await self._seller_marketplace_flow().render_listing_create_shop_picker(
                    seller_user_id=seller.user_id,
                )
            )
            return
        if action == "listing_create_prompt":
            await self._refresh_display_rub_per_usdt()
            await apply(
                await self._seller_marketplace_flow().start_listing_create_prompt(
                    seller_user_id=seller.user_id,
                    shop_id=entity_id_int(),
                )
            )
            return
        if action == "listing_open":
            await apply(
                await self._seller_marketplace_flow().render_listing_detail(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action == "listing_activation_blocked":
            listing_id = entity_id_int()
            if listing_id is not None:
                await apply(
                    await self._seller_marketplace_flow().render_listing_detail(
                        seller_user_id=seller.user_id,
                        listing_id=listing_id,
                        list_page=self._seller_listings_page_from_context(context),
                    )
                )
                return
            await self._replace_message(query_message, "Не удалось открыть карточку объявления. Попробуйте еще раз.")
            return
        if action == "listing_title_keep":
            prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
            if not isinstance(prompt_state, dict):
                await self._replace_message(
                    query_message,
                    "Не удалось продолжить создание объявления. Откройте раздел заново.",
                )
                return
            result = await self._get_seller_listing_creation_flow().create_draft_from_prompt(
                prompt_state=prompt_state,
            )
            await apply(result)
            return
        if action == "listing_title_edit_prompt":
            prompt_state = context.user_data.get(_PROMPT_STATE_KEY)
            if not isinstance(prompt_state, dict):
                await self._replace_message(
                    query_message,
                    "Не удалось продолжить создание объявления. Откройте раздел заново.",
                )
                return
            await apply(self._get_seller_listing_creation_flow().title_edit_prompt(prompt_state=prompt_state))
            return
        if action == "listing_edit":
            await apply(
                self._seller_marketplace_flow().render_listing_edit_disabled(
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action in {
            "listing_edit_title",
            "listing_edit_search",
            "listing_edit_cashback",
            "listing_edit_slots",
            "listing_edit_confirm",
        }:
            await apply(self._seller_marketplace_flow().render_listing_edit_field_disabled())
            return
        if action == "listing_activate":
            await apply(
                await self._seller_marketplace_flow().execute_listing_activate(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action == "listing_pause":
            await apply(
                await self._seller_marketplace_flow().execute_listing_pause(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action == "listing_unpause":
            await apply(
                await self._seller_marketplace_flow().execute_listing_unpause(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action == "listing_delete_preview":
            await apply(
                await self._seller_marketplace_flow().render_listing_delete_preview(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                )
            )
            return
        if action == "listing_delete_confirm":
            await apply(
                await self._seller_marketplace_flow().execute_listing_delete(
                    seller_user_id=seller.user_id,
                    listing_id=entity_id_int(),
                    list_page=self._seller_listings_page_from_context(context),
                )
            )
            return
        if action == "balance":
            await self._refresh_display_rub_per_usdt()
            await apply(await self._seller_marketplace_flow().render_balance(seller_user_id=seller.user_id))
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
            await apply(self._seller_marketplace_flow().start_topup_prompt(seller_user_id=seller.user_id))
            return
        if action == "topup_history":
            await apply(
                await self._seller_marketplace_flow().render_transaction_history(
                    seller_user_id=seller.user_id,
                    page=self._coerce_page_number(payload.entity_id),
                )
            )
            return
        if action == "topup_help":
            await apply(self._seller_marketplace_flow().render_topup_help())
            return

        await self._replace_message(
            query_message,
            "Неизвестное действие продавца.",
            self._flow_buttons_markup(self._seller_marketplace_flow().menu_buttons()),
        )

    @staticmethod
    def _coerce_page_number(raw_value: str | None) -> int:
        if not raw_value:
            return 1
        try:
            page = int(raw_value)
        except (TypeError, ValueError):
            return 1
        return page if page > 0 else 1

    def _seller_listings_page_from_context(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        return self._coerce_page_number(str(context.user_data.get(_SELLER_LISTINGS_PAGE_KEY, "1")))

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
                context=context,
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
                context=context,
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                page=self._coerce_page_number(payload.entity_id),
            )
            return
        if action == "kb_guide":
            await self._render_buyer_knowledge_screen(context=context, query_message=query_message, topic="guide")
            return
        if action == "kb_shops":
            await self._render_buyer_knowledge_screen(context=context, query_message=query_message, topic="shops")
            return
        if action == "kb_purchases":
            await self._render_buyer_knowledge_screen(
                context=context,
                query_message=query_message,
                topic="purchases",
            )
            return
        if action == "kb_balance":
            await self._render_buyer_knowledge_screen(context=context, query_message=query_message, topic="balance")
            return
        if action == "shop_page":
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            await self._refresh_display_rub_per_usdt()
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().open_shop_page(
                    buyer_user_id=buyer.user_id,
                    last_shop_slug=slug,
                    page=self._coerce_page_number(payload.entity_id),
                ),
            )
            return
        if action == "open_last_shop":
            slug = str(context.user_data.get(_LAST_BUYER_SHOP_SLUG_KEY, "")).strip()
            await self._refresh_display_rub_per_usdt()
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().open_last_shop(
                    buyer_user_id=buyer.user_id,
                    last_shop_slug=slug,
                ),
            )
            return
        if action == "open_saved_shop":
            try:
                shop_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                shop_id = None
            await self._refresh_display_rub_per_usdt()
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().open_saved_shop(
                    buyer_user_id=buyer.user_id,
                    shop_id=shop_id,
                ),
            )
            return
        if action == "shop_remove":
            try:
                shop_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                shop_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().remove_saved_shop(
                    buyer_user_id=buyer.user_id,
                    shop_id=shop_id,
                ),
            )
            return
        if action == "prompt_shop_slug":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=self._buyer_marketplace_flow().start_shop_slug_prompt(),
            )
            return
        if action == "listing_open":
            try:
                listing_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                listing_id = None
            if listing_id is None:
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
                context=context,
                query_message=query_message,
                buyer_user_id=buyer.user_id,
                shop_slug=slug,
                listing_id=listing_id,
            )
            return
        if action == "reserve":
            try:
                listing_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                listing_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().reserve_listing(
                    buyer_user_id=buyer.user_id,
                    listing_id=listing_id,
                    callback_query_id=callback_query_id,
                ),
            )
            return
        if action == "assignments":
            await self._render_buyer_assignments(
                context=context,
                query_message=query_message,
                buyer_user_id=buyer.user_id,
            )
            return
        if action == "submit_payload_prompt":
            try:
                assignment_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                assignment_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=self._buyer_marketplace_flow().start_purchase_payload_prompt(assignment_id=assignment_id),
            )
            return
        if action == "submit_review_payload_prompt":
            try:
                assignment_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                assignment_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().start_review_instruction(
                    buyer_user_id=buyer.user_id,
                    assignment_id=assignment_id,
                ),
            )
            return
        if action == "submit_review_payload_input_prompt":
            try:
                assignment_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                assignment_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=self._buyer_marketplace_flow().start_review_payload_prompt(assignment_id=assignment_id),
            )
            return
        if action == "assignment_cancel_prompt":
            try:
                assignment_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                assignment_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().start_assignment_cancel_prompt(
                    buyer_user_id=buyer.user_id,
                    assignment_id=assignment_id,
                ),
            )
            return
        if action == "assignment_cancel_confirm":
            try:
                assignment_id = int(payload.entity_id) if payload.entity_id else None
            except ValueError:
                assignment_id = None
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().confirm_assignment_cancel(
                    buyer_user_id=buyer.user_id,
                    assignment_id=assignment_id,
                    callback_query_id=callback_query_id,
                ),
            )
            return
        if action == "balance":
            await self._render_buyer_balance(
                context=context,
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
                context=context,
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
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_dashboard(buyer_user_id=buyer_user_id),
        )

    async def _render_buyer_knowledge_screen(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        topic: str,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=self._buyer_marketplace_flow().render_knowledge_screen(topic=topic),
        )

    async def _render_buyer_shops_section(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
        page: int = 1,
        notice: str | None = None,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_shops_section(
                buyer_user_id=buyer_user_id,
                page=page,
                notice=notice,
            ),
        )

    async def _execute_buyer_saved_shop_remove(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
        shop_id: int,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().remove_saved_shop(
                buyer_user_id=buyer_user_id,
                shop_id=shop_id,
            ),
        )

    async def _render_buyer_listing_detail(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
        shop_slug: str,
        listing_id: int,
        notice: str | None = None,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_listing_detail(
                buyer_user_id=buyer_user_id,
                shop_slug=shop_slug,
                listing_id=listing_id,
                notice=notice,
            ),
        )

    async def _render_buyer_assignments(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_assignments(buyer_user_id=buyer_user_id),
        )

    async def _render_buyer_balance(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_balance(buyer_user_id=buyer_user_id),
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
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
        buyer_user_id: int,
        page: int = 1,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_withdrawal_history(
                buyer_user_id=buyer_user_id,
                page=page,
            ),
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

        text = screen_text(
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
            screen_text(
                title="Выводы",
                cta="Выберите действие ниже.",
                lines=["Раздел для обработки и проверки заявок на вывод."],
                note=("Откройте ожидающие заявки, историю или перейдите к конкретной заявке по коду или номеру."),
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text=button_label_with_count("📋 Ожидают обработки", pending_count),
                            callback_data=build_callback(
                                flow=_ROLE_ADMIN,
                                action="withdrawals",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=button_label_with_count("🧾 История выводов", history_count),
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
            screen_text(
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
                            text=button_label_with_count("⚠️ Нужна проверка", exceptions_count),
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
                screen_text(
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
                f"{entity_block_heading_with_ref(label='Заявка', ref=withdraw_ref)}\n"
                f"Роль: {self._withdraw_requester_label(item.requester_role)}\n"
                f"Telegram: {item.requester_telegram_id} "
                f"(@{html.escape(item.requester_username or '-')})\n"
                f"Сумма: {format_usdt_value(item.amount_usdt, precise=True)} USDT\n"
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
            screen_text(title="Ожидают обработки", lines=lines),
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
                screen_text(
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

        resolved_page, total_pages, start_index, end_index = resolve_numbered_page(
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
                entity_block_heading_with_ref(label="Заявка", ref=withdraw_ref),
                f"Роль: {self._withdraw_requester_label(item.requester_role)}",
                (f"Telegram: {item.requester_telegram_id} (@{html.escape(item.requester_username or '-')})"),
                f"Сумма: {format_usdt_value(item.amount_usdt, precise=True)} USDT",
                f"Статус: {withdraw_status_badge(item.status)}",
                f"Кошелек: {html.escape(item.payout_address)}",
                f"Создана: {format_datetime_msk(item.requested_at)}",
            ]
            if item.processed_at:
                block_lines.append(f"Обработана: {format_datetime_msk(item.processed_at)}")
            if item.sent_at:
                block_lines.append(f"Отправлена: {format_datetime_msk(item.sent_at)}")
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
            screen_text(
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
            f"<b>Сумма:</b> {format_usdt_value(detail.amount_usdt, precise=True)} USDT",
            f"<b>Статус:</b> {withdraw_status_badge(detail.status)}",
            f"<b>Кошелек:</b> {html.escape(detail.payout_address)}",
            f"<b>Создана:</b> {format_datetime_msk(detail.requested_at)}",
            (
                f"<b>Обработана:</b> {format_datetime_msk(detail.processed_at)}"
                if detail.processed_at
                else "<b>Обработана:</b> -"
            ),
            (
                f"<b>Отправлена:</b> {format_datetime_msk(detail.sent_at)}"
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
            screen_text(
                title="Заявка",
                title_suffix_html=title_ref_suffix(self._withdrawal_ref(detail.withdrawal_request_id)),
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
        context: ContextTypes.DEFAULT_TYPE,
        query_message: Message | None,
    ) -> None:
        await self._apply_transport_effects(
            context=context,
            query_message=query_message,
            message=None,
            default_role=_ROLE_ADMIN,
            result=await self._admin_exceptions_flow().render_queue(),
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
        return f"{subject} {withdraw_ref}: {humanize_withdraw_status(status)}."

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
            await self._render_admin_deposit_exceptions(context=context, query_message=query_message)
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
            await self._render_admin_deposit_exceptions(context=context, query_message=query_message)
            return
        if action == "review_verify_prompt":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_ADMIN,
                result=self._admin_exceptions_flow().start_review_verification_prompt(
                    admin_user_id=admin_user_id,
                ),
            )
            return
        if action == "deposit_attach_prompt":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_ADMIN,
                result=self._admin_exceptions_flow().start_deposit_attach_prompt(
                    admin_user_id=admin_user_id,
                ),
            )
            return
        if action == "deposit_cancel_prompt":
            await self._apply_transport_effects(
                context=context,
                query_message=query_message,
                message=None,
                default_role=_ROLE_ADMIN,
                result=self._admin_exceptions_flow().start_deposit_cancel_prompt(
                    admin_user_id=admin_user_id,
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
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().submit_shop_create_token(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_shop_title_after_token":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().submit_shop_title_after_token(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_shop_token":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().submit_shop_token(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_shop_rename":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().submit_shop_rename(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "seller_topup_amount":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=await self._seller_marketplace_flow().submit_topup_amount(
                    prompt_state=prompt_state,
                    text=text,
                    update_id=update.update_id,
                ),
            )
            return

        if prompt_type == "seller_listing_create":
            seller_user_id = int(prompt_state.get("seller_user_id", 0))
            shop_id = int(prompt_state.get("shop_id", 0))
            shop_title = str(prompt_state.get("shop_title", "магазин"))
            if seller_user_id < 1 or shop_id < 1:
                await self._apply_transport_effects(
                    context=context,
                    query_message=None,
                    message=message,
                    default_role=_ROLE_SELLER,
                    result=FlowResult(
                        effects=(
                            ClearPrompt(),
                            ReplyText(
                                text=(
                                    "Не удалось продолжить создание объявления. "
                                    "Откройте раздел «📦 Объявления» заново."
                                ),
                                buttons=(
                                    (
                                        ButtonSpec(
                                            text="↩️ К объявлениям",
                                            flow=_ROLE_SELLER,
                                            action="listings",
                                        ),
                                    ),
                                ),
                                parse_mode=None,
                            ),
                        )
                    ),
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
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_SELLER,
                result=self._get_seller_listing_creation_flow().title_review_reminder(),
            )
            return

        if prompt_type in {"seller_listing_edit_value", "seller_listing_edit_confirm"}:
            self._clear_prompt(context)
            await message.reply_text(
                screen_text(
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

        if prompt_type == "buyer_shop_slug":
            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            await self._refresh_display_rub_per_usdt()
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().submit_shop_slug(
                    buyer_user_id=buyer.user_id,
                    slug=text,
                ),
            )
            return

        if prompt_type == "buyer_submit_payload":
            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().submit_purchase_payload(
                    prompt_state=prompt_state,
                    text=text,
                    buyer_user_id=buyer.user_id,
                    update_id=update.update_id,
                ),
            )
            return

        if prompt_type == "buyer_submit_review_payload":
            buyer = await self._buyer_service.bootstrap_buyer(
                telegram_id=identity.telegram_id,
                username=identity.username,
            )
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_BUYER,
                result=await self._buyer_marketplace_flow().submit_review_payload(
                    prompt_state=prompt_state,
                    text=text,
                    buyer_user_id=buyer.user_id,
                    update_id=update.update_id,
                ),
            )
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
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_ADMIN,
                result=await self._admin_exceptions_flow().submit_review_verification(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "admin_deposit_attach":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_ADMIN,
                result=await self._admin_exceptions_flow().submit_deposit_attach(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        if prompt_type == "admin_deposit_cancel":
            await self._apply_transport_effects(
                context=context,
                query_message=None,
                message=message,
                default_role=_ROLE_ADMIN,
                result=await self._admin_exceptions_flow().submit_deposit_cancel(
                    prompt_state=prompt_state,
                    text=text,
                ),
            )
            return

        self._clear_prompt(context)
        await message.reply_text("Неизвестный тип ввода. Отправьте /start.")

    def _get_seller_listing_creation_flow(self) -> SellerListingCreationFlow:
        if (
            self._seller_listing_creation_flow is not None
            and self._seller_listing_creation_flow_rate == self._display_rub_per_usdt
        ):
            return self._seller_listing_creation_flow
        seller_workflow = self._seller_workflow_service or _RuntimeSellerListingWorkflowAdapter(self)
        self._seller_listing_creation_flow = SellerListingCreationFlow(
            seller_service=self._seller_service,
            seller_workflow=seller_workflow,
            display_rub_per_usdt=self._display_rub_per_usdt,
            fx_rate_service=self._fx_rate_service,
            fx_rate_ttl_seconds=self._settings.fx_rate_ttl_seconds,
            listing_deep_link_builder=self._build_listing_deep_link,
        )
        self._seller_listing_creation_flow_rate = self._display_rub_per_usdt
        return self._seller_listing_creation_flow

    def _seller_marketplace_flow(self) -> SellerMarketplaceFlow:
        if (
            self._seller_marketplace_flow_cache is not None
            and self._seller_marketplace_flow_rate == self._display_rub_per_usdt
        ):
            return self._seller_marketplace_flow_cache
        self._seller_marketplace_flow_cache = SellerMarketplaceFlow(
            seller_service=self._seller_service,
            seller_workflow=self._seller_workflow_service,
            finance_service=self._finance_service,
            deposit_service=self._deposit_service,
            wb_ping_client=self._wb_ping_client,
            listing_creation_flow=self._get_seller_listing_creation_flow(),
            listing_product_validator=self._validate_listing_product_availability,
            config=SellerMarketplaceFlowConfig(
                display_rub_per_usdt=self._display_rub_per_usdt,
                telegram_bot_username=self._settings.telegram_bot_username,
                token_cipher_key=self._settings.token_cipher_key,
                seller_collateral_shard_key=self._settings.seller_collateral_shard_key,
                seller_collateral_invoice_ttl_hours=self._settings.seller_collateral_invoice_ttl_hours,
                tonapi_usdt_jetton_master=self._settings.tonapi_usdt_jetton_master,
                telegram_wallet_open_url=self._settings.telegram_wallet_open_url,
                support_bot_username=self._settings.support_bot_username,
                seller_listings_page_key=_SELLER_LISTINGS_PAGE_KEY,
            ),
        )
        self._seller_marketplace_flow_rate = self._display_rub_per_usdt
        return self._seller_marketplace_flow_cache

    def _seller_withdrawal_creation_flow(self) -> WithdrawalRequestCreationFlow:
        if self._seller_withdrawal_creation_flow_cache is not None:
            return self._seller_withdrawal_creation_flow_cache
        self._seller_withdrawal_creation_flow_cache = WithdrawalRequestCreationFlow(
            config=SELLER_WITHDRAWAL_CONFIG,
            requester_adapter=_RuntimeSellerWithdrawalAdapter(self),
            address_validator=_RuntimeTonMainnetAddressValidator(self),
        )
        return self._seller_withdrawal_creation_flow_cache

    def _buyer_withdrawal_creation_flow(self) -> WithdrawalRequestCreationFlow:
        if self._buyer_withdrawal_creation_flow_cache is not None:
            return self._buyer_withdrawal_creation_flow_cache
        self._buyer_withdrawal_creation_flow_cache = WithdrawalRequestCreationFlow(
            config=BUYER_WITHDRAWAL_CONFIG,
            requester_adapter=_RuntimeBuyerWithdrawalAdapter(self),
            address_validator=_RuntimeTonMainnetAddressValidator(self),
        )
        return self._buyer_withdrawal_creation_flow_cache

    def _buyer_marketplace_flow(self) -> BuyerMarketplaceFlow:
        if (
            self._buyer_marketplace_flow_cache is not None
            and self._buyer_marketplace_flow_rate == self._display_rub_per_usdt
        ):
            return self._buyer_marketplace_flow_cache
        self._buyer_marketplace_flow_cache = BuyerMarketplaceFlow(
            adapter=_RuntimeBuyerMarketplaceAdapter(self),
            config=BuyerMarketplaceFlowConfig(
                display_rub_per_usdt=self._display_rub_per_usdt,
                support_bot_username=self._settings.support_bot_username,
                last_shop_slug_key=_LAST_BUYER_SHOP_SLUG_KEY,
            ),
        )
        self._buyer_marketplace_flow_rate = self._display_rub_per_usdt
        return self._buyer_marketplace_flow_cache

    def _admin_exceptions_flow(self) -> AdminExceptionsFlow:
        if self._admin_exceptions_flow_cache is None:
            self._admin_exceptions_flow_cache = AdminExceptionsFlow(adapter=_RuntimeAdminExceptionsAdapter(self))
        return self._admin_exceptions_flow_cache

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
            if isinstance(effect, SetUserData):
                context.user_data[effect.key] = effect.value
                continue
            if isinstance(effect, AnswerCallback):
                if callback_query is not None:
                    try:
                        await callback_query.answer(
                            text=effect.text,
                            show_alert=effect.show_alert,
                        )
                    except Exception as exc:
                        self._logger.warning(
                            "telegram_callback_answer_failed",
                            error_type=type(exc).__name__,
                            error_message=str(exc)[:300],
                            source="transport_effect",
                        )
                        self._record_metric(
                            TELEGRAM_CALLBACK_ANSWER_FAILURE_METRIC,
                            {
                                "flow": default_role,
                                "action": "transport_effect",
                                "error_type": type(exc).__name__,
                            },
                        )
                else:
                    self._warn_dropped_transport_effect(
                        effect=effect,
                        reason="missing_callback_query",
                        default_role=default_role,
                    )
                continue
            if isinstance(effect, DeleteSourceMessage):
                target = message or query_message
                if target is not None:
                    await self._delete_sensitive_message(target, notify=False)
                continue
            if isinstance(effect, ReplyPhoto):
                target = message or query_message
                if target is None:
                    self._warn_dropped_transport_effect(
                        effect=effect,
                        reason="missing_message",
                        default_role=default_role,
                    )
                else:
                    await self._reply_with_photo_if_available(
                        target,
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
                else:
                    self._warn_dropped_transport_effect(
                        effect=effect,
                        reason="missing_message",
                        default_role=default_role,
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
                else:
                    self._warn_dropped_transport_effect(
                        effect=effect,
                        reason="missing_message",
                        default_role=default_role,
                    )
                continue
            if isinstance(effect, ReplaceText):
                target = query_message or message
                if target is None:
                    self._warn_dropped_transport_effect(
                        effect=effect,
                        reason="missing_message",
                        default_role=default_role,
                    )
                    continue
                await self._replace_message(
                    target,
                    effect.text,
                    self._flow_buttons_markup(effect.buttons),
                    parse_mode=effect.parse_mode,
                )
                continue
            if isinstance(effect, LogEvent):
                self._logger.info(effect.event_name, **effect.fields)

    def _warn_dropped_transport_effect(
        self,
        *,
        effect: object,
        reason: str,
        default_role: str,
    ) -> None:
        self._logger.warning(
            "telegram_transport_effect_dropped",
            effect_type=type(effect).__name__,
            reason=reason,
            default_role=default_role,
        )

    def _role_menu_markup(self, role: str) -> InlineKeyboardMarkup:
        if role == _ROLE_SELLER:
            markup = self._flow_buttons_markup(self._seller_marketplace_flow().menu_buttons())
            if markup is None:
                raise ValueError("seller menu has no buttons")
            return markup
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
        context: ContextTypes.DEFAULT_TYPE,
        slug: str,
        buyer_user_id: int | None = None,
        prefer_edit: bool = False,
        page: int = 1,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=message if prefer_edit else None,
            message=None if prefer_edit else message,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().render_shop_catalog(
                slug=slug,
                buyer_user_id=buyer_user_id,
                replace=prefer_edit,
                page=page,
            ),
        )

    async def _send_buyer_listing_deep_link(
        self,
        message: Message | None,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        listing_id: int,
        buyer_user_id: int,
    ) -> None:
        await self._refresh_display_rub_per_usdt()
        await self._apply_transport_effects(
            context=context,
            query_message=None,
            message=message,
            default_role=_ROLE_BUYER,
            result=await self._buyer_marketplace_flow().open_listing_deep_link(
                buyer_user_id=buyer_user_id,
                listing_id=listing_id,
            ),
        )

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

    def _format_usdt_with_rub(self, amount: Decimal, *, precise: bool = False) -> str:
        return format_usdt_with_rub(
            amount,
            display_rub_per_usdt=self._display_rub_per_usdt,
            precise=precise,
        )

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

    def _format_cashback_with_percent(
        self,
        *,
        reward_usdt: Decimal,
        reference_price_rub: int | None,
    ) -> str:
        return format_cashback_with_percent(
            reward_usdt=reward_usdt,
            reference_price_rub=reference_price_rub,
            display_rub_per_usdt=self._display_rub_per_usdt,
        )

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
    def _shop_ref(shop_id: int) -> str:
        return format_shop_ref(shop_id)

    @staticmethod
    def _listing_ref(listing_id: int) -> str:
        return format_listing_ref(listing_id)

    @staticmethod
    def _assignment_ref(assignment_id: int) -> str:
        return format_assignment_ref(assignment_id)

    @staticmethod
    def _withdrawal_ref(withdrawal_request_id: int) -> str:
        return format_withdrawal_ref(withdrawal_request_id)

    @staticmethod
    def _deposit_ref(deposit_intent_id: int) -> str:
        return format_deposit_ref(deposit_intent_id)

    @staticmethod
    def _parse_withdrawal_reference(value: str) -> int:
        return parse_withdrawal_ref(value)

    def _build_listing_deep_link(self, listing_id: int) -> str:
        return build_listing_deep_link(
            bot_username=self._settings.telegram_bot_username,
            listing_id=listing_id,
        )

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
        spec = self._knowledge_button_spec(role=role, topic=topic)
        return InlineKeyboardButton(
            text=spec.text,
            callback_data=build_callback(flow=str(spec.flow), action=str(spec.action)),
        )

    def _knowledge_button_spec(self, *, role: str, topic: str) -> ButtonSpec:
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
        return ButtonSpec(text=label, flow=role, action=action)

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

    @staticmethod
    def _humanize_listing_status(status: str) -> str:
        mapping = {
            "draft": "Черновик",
            "active": "Активно",
            "paused": "На паузе",
        }
        return mapping.get(status, status)

    def _listing_activity_badge(self, *, is_active: bool) -> str:
        return status_badge(
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
        return status_badge(label, color=color)

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
        if not _is_http_url(photo_url):
            await self._try_reply_photo(
                message,
                photo=photo_url,
                source_url=photo_url,
                strategy="telegram_file_id_or_url",
            )
            return
        should_skip_direct_url = _is_webp_url(photo_url) and _is_wb_photo_url(photo_url)
        if not should_skip_direct_url and await self._try_reply_photo(
            message,
            photo=photo_url,
            source_url=photo_url,
            strategy="direct_url",
        ):
            return

        downloaded = await self._download_photo_for_upload(photo_url)
        if downloaded is None:
            return

        # Keep logs tied to the original product URL, but name the upload from the resolved response URL.
        upload_filename = _photo_upload_filename(photo_url=downloaded.final_url, content_type=downloaded.content_type)
        upload = InputFile(downloaded.data, filename=upload_filename)
        if await self._try_reply_photo(
            message,
            photo=upload,
            source_url=photo_url,
            strategy="memory_upload",
        ):
            return

        try:
            jpeg_data = await asyncio.to_thread(_convert_image_bytes_to_jpeg, downloaded.data)
        except Exception as exc:
            self._logger.warning(
                "telegram_photo_conversion_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                photo_url=photo_url,
                content_type=downloaded.content_type,
            )
            return

        await self._try_reply_photo(
            message,
            photo=InputFile(jpeg_data, filename="listing.jpg"),
            source_url=photo_url,
            strategy="jpeg_memory_upload",
        )

    async def _download_photo_for_upload(self, photo_url: str) -> _DownloadedPhoto | None:
        if not _is_wb_photo_url(photo_url):
            return None
        try:
            return await asyncio.to_thread(_download_photo_from_url, photo_url)
        except _PhotoDownloadError as exc:
            self._logger.warning(
                "telegram_photo_download_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                photo_url=photo_url,
            )
            return None
        except Exception as exc:
            self._logger.warning(
                "telegram_photo_download_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                photo_url=photo_url,
            )
            return None

    async def _try_reply_photo(
        self,
        message: Message,
        *,
        photo: str | InputFile,
        source_url: str,
        strategy: str,
    ) -> bool:
        try:
            await message.reply_photo(photo=photo)
            return True
        except Exception as exc:
            self._logger.warning(
                "telegram_photo_reply_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                photo_url=source_url,
                strategy=strategy,
            )
            return False

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

    def _buyer_menu_markup(
        self,
        *,
        shops_count: int | None = None,
        purchases_count: int | None = None,
    ) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(
                    text=button_label_with_count("🏪 Магазины", shops_count),
                    callback_data=build_callback(
                        flow=_ROLE_BUYER,
                        action="shops",
                    ),
                ),
                InlineKeyboardButton(
                    text=button_label_with_count("📋 Покупки", purchases_count),
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
                        text=button_label_with_count("💸 Выводы", pending_withdrawals_count),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="withdrawals_section",
                        ),
                    ),
                    InlineKeyboardButton(
                        text=button_label_with_count("🏦 Депозиты", deposit_exceptions_count),
                        callback_data=build_callback(
                            flow=_ROLE_ADMIN,
                            action="deposits_section",
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=button_label_with_count("⚠️ Исключения", exceptions_count),
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

    def _record_update_metrics(
        self,
        update: object,
        *,
        handler: str,
        outcome: str,
        observed_at: datetime,
    ) -> None:
        update_type = self._telegram_update_type(update)
        labels = {
            "update_type": update_type,
            "handler": handler,
            "outcome": outcome,
        }
        self._record_metric(TELEGRAM_UPDATE_RECEIVED_METRIC, labels)
        delivery_timestamp = self._telegram_update_timestamp(update)
        if delivery_timestamp is None:
            return
        lag_seconds = max(0.0, (observed_at - delivery_timestamp).total_seconds())
        self._record_metric(TELEGRAM_UPDATE_DELIVERY_LAG_METRIC, labels, value=lag_seconds)

    def _record_metric(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        if not self._monitoring_recorder:
            return
        self._monitoring_recorder.record(name, labels, value)

    def _telegram_update_type(self, update: object) -> str:
        if getattr(update, "callback_query", None) is not None:
            return "callback_query"
        if getattr(update, "message", None) is not None:
            return "message"
        return "unknown"

    def _telegram_update_timestamp(self, update: object) -> datetime | None:
        message = getattr(update, "message", None)
        if message is None:
            callback_query = getattr(update, "callback_query", None)
            message = getattr(callback_query, "message", None)
        raw_date = getattr(message, "date", None)
        if isinstance(raw_date, datetime):
            if raw_date.tzinfo is None:
                return raw_date.replace(tzinfo=UTC)
            return raw_date.astimezone(UTC)
        if isinstance(raw_date, (int, float)):
            return datetime.fromtimestamp(raw_date, tz=UTC)
        return None

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
                "WEBHOOK_BASE_URL is required when TELEGRAM_UPDATE_MODE=webhook "
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
