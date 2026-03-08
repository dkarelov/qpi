from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo


_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class WbPublicApiError(Exception):
    status_code: int | None
    message: str

    def __str__(self) -> str:
        prefix = str(self.status_code) if self.status_code is not None else "network"
        return f"{prefix}: {self.message}"


@dataclass(frozen=True)
class WbObservedBuyerPrice:
    buyer_price_rub: int
    seller_price_rub: int
    spp_percent: int
    observed_at: datetime | None
    source: str = "orders"


@dataclass(frozen=True)
class WbProductSnapshot:
    wb_product_id: int
    subject_name: str | None
    vendor_code: str | None
    brand: str | None
    name: str
    description: str | None
    photo_url: str | None
    tech_sizes: list[str]
    characteristics: list[dict[str, str]]


class WbPublicCatalogClient:
    """Seller-token WB client for product cards and recent order pricing."""

    def __init__(
        self,
        *,
        content_endpoint: str = "https://content-api.wildberries.ru/content/v2/get/cards/list",
        orders_endpoint: str = "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
        content_timeout_seconds: int = 10,
        orders_timeout_seconds: int = 20,
        orders_lookback_days: int = 30,
        content_min_interval_seconds: float = 0.6,
        orders_min_interval_seconds: float = 60.0,
        retry_count: int = 2,
    ) -> None:
        self._content_endpoint = content_endpoint
        self._orders_endpoint = orders_endpoint
        self._content_timeout_seconds = content_timeout_seconds
        self._orders_timeout_seconds = orders_timeout_seconds
        self._orders_lookback_days = orders_lookback_days
        self._content_min_interval_seconds = content_min_interval_seconds
        self._orders_min_interval_seconds = orders_min_interval_seconds
        self._retry_count = retry_count
        self._lock = asyncio.Lock()
        self._last_request_at_by_key: dict[tuple[str, str], float] = {}

    async def fetch_product_snapshot(
        self,
        *,
        token: str,
        wb_product_id: int,
    ) -> WbProductSnapshot:
        if wb_product_id < 1:
            raise ValueError("wb_product_id must be >= 1")
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("token must not be empty")

        payload = await self._fetch_content_json_with_retries(
            token=normalized_token,
            wb_product_id=wb_product_id,
        )
        cards = payload.get("cards")
        if not isinstance(cards, list):
            raise WbPublicApiError(status_code=200, message="content cards payload is malformed")
        card = next(
            (
                item
                for item in cards
                if isinstance(item, dict) and _to_int(item.get("nmID")) == wb_product_id
            ),
            None,
        )
        if card is None:
            raise WbPublicApiError(status_code=404, message="product card not found for seller")

        title = str(card.get("title") or "").strip()
        if not title:
            raise WbPublicApiError(status_code=200, message="product title is missing")

        photos = card.get("photos")
        photo_url: str | None = None
        if isinstance(photos, list):
            for photo in photos:
                if not isinstance(photo, dict):
                    continue
                for key in ("c516x688", "big", "c246x328", "square", "tm"):
                    value = _normalize_optional_text(photo.get(key))
                    if value:
                        photo_url = value
                        break
                if photo_url:
                    break

        sizes: list[str] = []
        seen_sizes: set[str] = set()
        raw_sizes = card.get("sizes")
        if isinstance(raw_sizes, list):
            for size in raw_sizes:
                if not isinstance(size, dict):
                    continue
                tech_size = _normalize_optional_text(size.get("techSize"))
                if not tech_size or tech_size in seen_sizes:
                    continue
                seen_sizes.add(tech_size)
                sizes.append(tech_size)

        characteristics: list[dict[str, str]] = []
        raw_characteristics = card.get("characteristics")
        if isinstance(raw_characteristics, list):
            for item in raw_characteristics:
                if not isinstance(item, dict):
                    continue
                name = _normalize_optional_text(item.get("name"))
                if not name:
                    continue
                value = _stringify_characteristic_value(item.get("value"))
                if not value:
                    continue
                characteristics.append({"name": name, "value": value})

        return WbProductSnapshot(
            wb_product_id=wb_product_id,
            subject_name=_normalize_optional_text(card.get("subjectName")),
            vendor_code=_normalize_optional_text(card.get("vendorCode")),
            brand=_normalize_optional_text(card.get("brand")),
            name=title,
            description=_normalize_optional_text(card.get("description")),
            photo_url=photo_url,
            tech_sizes=sizes,
            characteristics=characteristics,
        )

    async def lookup_buyer_price(
        self,
        *,
        token: str,
        wb_product_id: int,
        lookback_days: int | None = None,
    ) -> WbObservedBuyerPrice | None:
        if wb_product_id < 1:
            raise ValueError("wb_product_id must be >= 1")
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("token must not be empty")

        effective_lookback_days = lookback_days or self._orders_lookback_days
        if effective_lookback_days < 1:
            raise ValueError("lookback_days must be >= 1")

        rows = await self._fetch_orders_json_with_retries(
            token=normalized_token,
            date_from=datetime.now(_MOSCOW_TZ) - timedelta(days=effective_lookback_days),
        )
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and _to_int(row.get("nmId")) == wb_product_id
        ]
        if not matches:
            return None

        preferred = [row for row in matches if not bool(row.get("isCancel"))]
        row = max(preferred or matches, key=_order_sort_key)

        price_with_disc = _to_decimal(row.get("priceWithDisc"))
        spp = _to_decimal(row.get("spp"))
        if price_with_disc is None or price_with_disc <= Decimal("0") or spp is None:
            return None

        buyer_price = (
            price_with_disc
            * (Decimal("100") - spp)
            / Decimal("100")
            * Decimal("0.97")
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        seller_price = price_with_disc.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return WbObservedBuyerPrice(
            buyer_price_rub=int(buyer_price),
            seller_price_rub=int(seller_price),
            spp_percent=int(spp.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            observed_at=_parse_order_datetime(row.get("lastChangeDate") or row.get("date")),
        )

    async def _fetch_content_json_with_retries(
        self,
        *,
        token: str,
        wb_product_id: int,
    ) -> dict[str, Any]:
        last_error: WbPublicApiError | None = None
        body = {
            "settings": {
                "filter": {
                    "textSearch": str(wb_product_id),
                    "withPhoto": -1,
                },
                "cursor": {"limit": 100},
            }
        }
        for attempt in range(1, self._retry_count + 1):
            await self._acquire_slot(
                key=("content", token),
                min_interval_seconds=self._content_min_interval_seconds,
            )
            try:
                return await asyncio.to_thread(
                    self._post_json_sync,
                    self._content_endpoint,
                    token,
                    body,
                    self._content_timeout_seconds,
                    {"locale": "ru"},
                )
            except WbPublicApiError as exc:
                last_error = exc
                if exc.status_code not in {429, 500, 502, 503, 504} or attempt == self._retry_count:
                    raise
                await asyncio.sleep(self._content_min_interval_seconds * attempt)

        assert last_error is not None
        raise last_error

    async def _fetch_orders_json_with_retries(
        self,
        *,
        token: str,
        date_from: datetime,
    ) -> list[dict[str, Any]]:
        last_error: WbPublicApiError | None = None
        params = {
            "flag": "0",
            "dateFrom": date_from.astimezone(_MOSCOW_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        for attempt in range(1, self._retry_count + 1):
            await self._acquire_slot(
                key=("orders", token),
                min_interval_seconds=self._orders_min_interval_seconds,
            )
            try:
                payload = await asyncio.to_thread(
                    self._get_json_sync,
                    self._orders_endpoint,
                    token,
                    self._orders_timeout_seconds,
                    params,
                )
                if isinstance(payload, list):
                    return [row for row in payload if isinstance(row, dict)]
                raise WbPublicApiError(status_code=200, message="orders payload is malformed")
            except WbPublicApiError as exc:
                last_error = exc
                if exc.status_code not in {429, 500, 502, 503, 504} or attempt == self._retry_count:
                    raise
                await asyncio.sleep(self._orders_min_interval_seconds * attempt)

        assert last_error is not None
        raise last_error

    async def _acquire_slot(
        self,
        *,
        key: tuple[str, str],
        min_interval_seconds: float,
    ) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                last_request_at = self._last_request_at_by_key.get(key)
                if last_request_at is None or now - last_request_at >= min_interval_seconds:
                    self._last_request_at_by_key[key] = now
                    return
                wait_for = min_interval_seconds - (now - last_request_at)
            await asyncio.sleep(max(wait_for, 0.05))

    def _post_json_sync(
        self,
        endpoint: str,
        token: str,
        body: dict[str, Any],
        timeout_seconds: int,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        encoded_query = f"?{urllib.parse.urlencode(query)}" if query else ""
        request = urllib.request.Request(
            f"{endpoint}{encoded_query}",
            method="POST",
            headers={
                "Authorization": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        return self._request_json_sync(request, timeout_seconds)

    def _get_json_sync(
        self,
        endpoint: str,
        token: str,
        timeout_seconds: int,
        query: dict[str, str] | None = None,
    ) -> Any:
        encoded_query = f"?{urllib.parse.urlencode(query)}" if query else ""
        request = urllib.request.Request(
            f"{endpoint}{encoded_query}",
            method="GET",
            headers={
                "Authorization": token,
                "Accept": "application/json",
            },
        )
        return self._request_json_sync(request, timeout_seconds)

    def _request_json_sync(self, request: urllib.request.Request, timeout_seconds: int) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = _extract_message(body) or body or str(exc.reason)
            raise WbPublicApiError(status_code=exc.code, message=message[:1000]) from exc
        except urllib.error.URLError as exc:
            raise WbPublicApiError(status_code=None, message=str(exc.reason)) from exc
        except TimeoutError as exc:
            raise WbPublicApiError(status_code=None, message="timeout") from exc

        if not 200 <= status < 300:
            raise WbPublicApiError(status_code=status, message="unexpected response status")
        if not payload.strip():
            raise WbPublicApiError(status_code=status, message="empty response body")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WbPublicApiError(status_code=status, message="invalid JSON response") from exc


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _stringify_characteristic_value(value: Any) -> str | None:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(parts) or None
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_order_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_MOSCOW_TZ).astimezone(UTC)
    return parsed.astimezone(UTC)


def _order_sort_key(row: dict[str, Any]) -> tuple[datetime, int]:
    parsed = _parse_order_datetime(row.get("lastChangeDate") or row.get("date"))
    return (parsed or datetime(1970, 1, 1, tzinfo=UTC), _to_int(row.get("nmId")) or 0)


def _extract_message(payload: str) -> str | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        for key in ("message", "error", "title", "detail", "statusText"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
    return None
