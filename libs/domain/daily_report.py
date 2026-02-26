from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.domain.seller import SellerService
from libs.integrations.wb_reports import WbReportApiError, WbReportClient
from libs.logging.setup import EventLogger, get_logger
from libs.security.token_cipher import decrypt_token

_SCRAPPER_WITHDRAWN_SOURCE = "scrapper_401_withdrawn"
_SCRAPPER_EXPIRED_SOURCE = "scrapper_401_token_expired"
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 522, 524}
_ALLOWED_SUPPLIER_OPER_NAMES = {
    "Возврат",
    "Продажа",
    "Коррекция продаж",
    "Коррекция возвратов",
}

_REPORT_COLUMN_NAMES = (
    "realizationreport_id",
    "create_dt",
    "currency_name",
    "rrd_id",
    "subject_name",
    "nm_id",
    "brand_name",
    "sa_name",
    "ts_name",
    "quantity",
    "retail_amount",
    "office_name",
    "supplier_oper_name",
    "order_dt",
    "sale_dt",
    "delivery_amount",
    "return_amount",
    "supplier_promo",
    "ppvz_office_name",
    "ppvz_office_id",
    "sticker_id",
    "site_country",
    "assembly_id",
    "srid",
    "order_uid",
    "delivery_method",
    "uuid_promocode",
    "sale_price_promocode_discount_prc",
)

_REPORT_UPSERT_QUERY = (
    "INSERT INTO wb_report_rows "
    f"({', '.join(_REPORT_COLUMN_NAMES)}) "
    f"VALUES ({', '.join(f'%({column})s' for column in _REPORT_COLUMN_NAMES)}) "
    "ON CONFLICT (rrd_id, srid) DO UPDATE SET "
    + ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _REPORT_COLUMN_NAMES
        if column not in {"rrd_id", "srid"}
    )
)


@dataclass(frozen=True)
class DailyReportRunResult:
    shops_total: int
    shops_processed: int
    shops_failed: int
    shops_invalidated: int
    rows_seen: int
    rows_upserted: int
    rows_skipped: int


@dataclass(frozen=True)
class _ShopToken:
    shop_id: int
    token_ciphertext: str


@dataclass(frozen=True)
class _ShopResult:
    shop_id: int
    processed: bool
    failed: bool
    invalidated: bool
    rows_seen: int
    rows_upserted: int
    rows_skipped: int
    pages_fetched: int
    final_rrd_id: int
    failure_stage: str | None = None
    failure_status_code: int | None = None
    failure_message: str | None = None
    invalidation_source: str | None = None


class DailyReportScrapperService:
    """Hourly `reportDetailByPeriod` sync into PostgreSQL."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        token_cipher_key: str,
        wb_client: WbReportClient,
        concurrency: int,
        request_limit: int,
        max_retries: int,
        retry_delay_seconds: float,
        days_back: int,
        seller_service: SellerService | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if request_limit < 1:
            raise ValueError("request_limit must be >= 1")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be > 0")
        if days_back < 1:
            raise ValueError("days_back must be >= 1")

        self._pool = pool
        self._token_cipher_key = token_cipher_key
        self._wb_client = wb_client
        self._concurrency = concurrency
        self._request_limit = request_limit
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._days_back = days_back
        self._seller_service = seller_service or SellerService(pool)
        self._logger = logger or get_logger(__name__)

    async def run_once(self) -> DailyReportRunResult:
        shops = await self._list_target_shops()
        if not shops:
            self._logger.info("daily_report_no_target_shops", shops_total=0)
            return DailyReportRunResult(
                shops_total=0,
                shops_processed=0,
                shops_failed=0,
                shops_invalidated=0,
                rows_seen=0,
                rows_upserted=0,
                rows_skipped=0,
            )

        today = datetime.now(UTC).date()
        # WB report endpoint returns finalized report rows only up to yesterday.
        date_to = today - timedelta(days=1)
        date_from = date_to - timedelta(days=self._days_back - 1)
        self._logger.info(
            "daily_report_sync_window",
            shops_total=len(shops),
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            concurrency=self._concurrency,
            request_limit=self._request_limit,
            max_retries=self._max_retries,
        )

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _run_shop(shop: _ShopToken) -> _ShopResult:
            async with semaphore:
                try:
                    return await self._process_shop(shop, date_from=date_from, date_to=date_to)
                except Exception as exc:
                    self._logger.exception(
                        "daily_report_shop_failed_unhandled",
                        shop_id=shop.shop_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                    return _ShopResult(
                        shop_id=shop.shop_id,
                        processed=False,
                        failed=True,
                        invalidated=False,
                        rows_seen=0,
                        rows_upserted=0,
                        rows_skipped=0,
                        pages_fetched=0,
                        final_rrd_id=0,
                        failure_stage="unhandled_exception",
                        failure_message=str(exc)[:500],
                    )

        shop_results = await asyncio.gather(*[_run_shop(shop) for shop in shops])

        return DailyReportRunResult(
            shops_total=len(shops),
            shops_processed=sum(1 for result in shop_results if result.processed),
            shops_failed=sum(1 for result in shop_results if result.failed),
            shops_invalidated=sum(1 for result in shop_results if result.invalidated),
            rows_seen=sum(result.rows_seen for result in shop_results),
            rows_upserted=sum(result.rows_upserted for result in shop_results),
            rows_skipped=sum(result.rows_skipped for result in shop_results),
        )

    async def _process_shop(
        self,
        shop: _ShopToken,
        *,
        date_from: date,
        date_to: date,
    ) -> _ShopResult:
        self._logger.info(
            "daily_report_shop_started",
            shop_id=shop.shop_id,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        try:
            token = decrypt_token(shop.token_ciphertext, self._token_cipher_key)
        except Exception as exc:
            self._logger.error(
                "daily_report_shop_failed_decrypt",
                shop_id=shop.shop_id,
                failure_stage="token_decrypt",
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            return _ShopResult(
                shop_id=shop.shop_id,
                processed=False,
                failed=True,
                invalidated=False,
                rows_seen=0,
                rows_upserted=0,
                rows_skipped=0,
                pages_fetched=0,
                final_rrd_id=0,
                failure_stage="token_decrypt",
                failure_message=str(exc)[:500],
            )

        current_rrd_id = 0
        rows_seen = 0
        rows_upserted = 0
        rows_skipped = 0
        pages_fetched = 0

        while True:
            try:
                page = await self._fetch_page_with_retry(
                    shop_id=shop.shop_id,
                    token=token,
                    date_from=date_from,
                    date_to=date_to,
                    rrd_id=current_rrd_id,
                )
                pages_fetched += 1
            except WbReportApiError as exc:
                invalidated, invalidation_source = await self._maybe_invalidate_token(
                    shop_id=shop.shop_id,
                    exc=exc,
                )
                log_method = (
                    self._logger.warning
                    if exc.status_code is not None and 400 <= exc.status_code < 500
                    else self._logger.error
                )
                log_method(
                    "daily_report_shop_failed_wb_api",
                    shop_id=shop.shop_id,
                    failure_stage="wb_api",
                    status_code=exc.status_code,
                    error_message=(exc.message or "")[:500],
                    invalidated=invalidated,
                    invalidation_source=invalidation_source,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                )
                return _ShopResult(
                    shop_id=shop.shop_id,
                    processed=False,
                    failed=True,
                    invalidated=invalidated,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                    failure_stage="wb_api",
                    failure_status_code=exc.status_code,
                    failure_message=(exc.message or "")[:500],
                    invalidation_source=invalidation_source,
                )

            if not page:
                self._logger.info(
                    "daily_report_shop_finished",
                    shop_id=shop.shop_id,
                    processed=True,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                )
                return _ShopResult(
                    shop_id=shop.shop_id,
                    processed=True,
                    failed=False,
                    invalidated=False,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                )

            rows_seen += len(page)
            projected_rows: list[dict[str, Any]] = []
            last_rrd_id = current_rrd_id

            for entry in page:
                rrd_id = _to_int(entry.get("rrd_id"))
                if rrd_id is not None and rrd_id > last_rrd_id:
                    last_rrd_id = rrd_id

                projected = project_report_row(entry)
                if projected is None:
                    rows_skipped += 1
                    continue
                projected_rows.append(projected)

            if projected_rows:
                rows_upserted += await self._upsert_rows(projected_rows)

            if len(page) < self._request_limit:
                self._logger.info(
                    "daily_report_shop_finished",
                    shop_id=shop.shop_id,
                    processed=True,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=last_rrd_id,
                )
                return _ShopResult(
                    shop_id=shop.shop_id,
                    processed=True,
                    failed=False,
                    invalidated=False,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=last_rrd_id,
                )

            if last_rrd_id <= current_rrd_id:
                self._logger.error(
                    "daily_report_shop_failed_pagination_stall",
                    shop_id=shop.shop_id,
                    failure_stage="pagination_stall",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                )
                return _ShopResult(
                    shop_id=shop.shop_id,
                    processed=False,
                    failed=True,
                    invalidated=False,
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    rows_skipped=rows_skipped,
                    pages_fetched=pages_fetched,
                    final_rrd_id=current_rrd_id,
                    failure_stage="pagination_stall",
                    failure_message="rrd_id did not advance",
                )

            current_rrd_id = last_rrd_id

    async def _fetch_page_with_retry(
        self,
        *,
        shop_id: int,
        token: str,
        date_from: date,
        date_to: date,
        rrd_id: int,
    ) -> list[dict[str, Any]]:
        attempt = 0
        while True:
            try:
                return await self._wb_client.fetch_report_detail_page(
                    token=token,
                    date_from=date_from,
                    date_to=date_to,
                    rrd_id=rrd_id,
                    limit=self._request_limit,
                )
            except WbReportApiError as exc:
                should_retry = (
                    exc.status_code is None or exc.status_code in _RETRYABLE_STATUS_CODES
                )
                if not should_retry or attempt >= self._max_retries:
                    raise
                delay = self._retry_delay_seconds * (2**attempt)
                self._logger.warning(
                    "daily_report_wb_retry_scheduled",
                    shop_id=shop_id,
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    status_code=exc.status_code,
                    error_message=(exc.message or "")[:500],
                    rrd_id=rrd_id,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def _list_target_shops(self) -> list[_ShopToken]:
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT s.id, s.wb_token_ciphertext
                    FROM shops s
                    WHERE s.deleted_at IS NULL
                      AND s.wb_token_status = 'valid'
                      AND s.wb_token_ciphertext IS NOT NULL
                      AND EXISTS (
                        SELECT 1
                        FROM listings l
                        WHERE l.shop_id = s.id
                          AND l.deleted_at IS NULL
                      )
                    ORDER BY s.id ASC
                    """
                )
                rows = await cur.fetchall()
                return [
                    _ShopToken(
                        shop_id=row["id"],
                        token_ciphertext=row["wb_token_ciphertext"],
                    )
                    for row in rows
                ]

    async def _upsert_rows(self, rows: list[dict[str, Any]]) -> int:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(_REPORT_UPSERT_QUERY, rows)
        return len(rows)

    async def _maybe_invalidate_token(
        self,
        *,
        shop_id: int,
        exc: WbReportApiError,
    ) -> tuple[bool, str | None]:
        source = classify_token_invalidation_source(exc.status_code, exc.message)
        if source is None:
            return False, None

        result = await self._seller_service.invalidate_shop_token_and_pause(
            shop_id=shop_id,
            source=source,
            error_message=(exc.message or "")[:500],
        )
        return result.changed, source


def classify_token_invalidation_source(status_code: int | None, message: str | None) -> str | None:
    if status_code != 401:
        return None

    normalized = (message or "").lower()
    if "withdrawn" in normalized:
        return _SCRAPPER_WITHDRAWN_SOURCE
    if "token expired" in normalized:
        return _SCRAPPER_EXPIRED_SOURCE
    return None


def project_report_row(row: dict[str, Any]) -> dict[str, Any] | None:
    rrd_id = _to_int(row.get("rrd_id"))
    srid = _to_text(row.get("srid"))
    supplier_oper_name = _to_text(row.get("supplier_oper_name"))
    if (
        rrd_id is None
        or srid is None
        or supplier_oper_name is None
        or supplier_oper_name not in _ALLOWED_SUPPLIER_OPER_NAMES
    ):
        return None

    return {
        "realizationreport_id": _to_int(row.get("realizationreport_id")),
        "create_dt": _to_datetime(row.get("create_dt")),
        "currency_name": _to_text(row.get("currency_name")),
        "rrd_id": rrd_id,
        "subject_name": _to_text(row.get("subject_name")),
        "nm_id": _to_int(row.get("nm_id")),
        "brand_name": _to_text(row.get("brand_name")),
        "sa_name": _to_text(row.get("sa_name")),
        "ts_name": _to_text(row.get("ts_name")),
        "quantity": _to_int(row.get("quantity")),
        "retail_amount": _to_decimal(row.get("retail_amount")),
        "office_name": _to_text(row.get("office_name")),
        "supplier_oper_name": supplier_oper_name,
        "order_dt": _to_datetime(row.get("order_dt")),
        "sale_dt": _to_datetime(row.get("sale_dt")),
        "delivery_amount": _to_int(row.get("delivery_amount")),
        "return_amount": _to_int(row.get("return_amount")),
        "supplier_promo": _to_decimal(row.get("supplier_promo")),
        "ppvz_office_name": _to_text(row.get("ppvz_office_name")),
        "ppvz_office_id": _to_int(row.get("ppvz_office_id")),
        "sticker_id": _to_text(row.get("sticker_id")),
        "site_country": _to_text(row.get("site_country")),
        "assembly_id": _to_int(row.get("assembly_id")),
        "srid": srid,
        "order_uid": _to_text(row.get("order_uid")),
        "delivery_method": _to_text(row.get("delivery_method")),
        "uuid_promocode": _to_text(row.get("uuid_promocode")),
        "sale_price_promocode_discount_prc": _to_decimal(
            row.get("sale_price_promocode_discount_prc")
        ),
    }


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
