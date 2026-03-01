from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TonapiApiError(Exception):
    status_code: int | None
    message: str

    def __str__(self) -> str:
        prefix = str(self.status_code) if self.status_code is not None else "network"
        return f"{prefix}: {self.message}"


@dataclass(frozen=True)
class TonapiAddressInfo:
    raw_form: str


@dataclass(frozen=True)
class TonapiJettonOperation:
    operation: str
    utime: datetime
    lt: int
    transaction_hash: str
    source_address: str | None
    destination_address: str | None
    amount_raw: str
    decimals: int
    query_id: str
    trace_id: str
    payload: dict[str, Any]

    @property
    def amount_usdt(self) -> Decimal:
        scale = Decimal(10) ** int(self.decimals)
        return Decimal(self.amount_raw) / scale


@dataclass(frozen=True)
class TonapiJettonHistoryPage:
    operations: list[TonapiJettonOperation]
    next_from: int | None


class TonapiClient:
    """Minimal TonAPI reader for USDT jetton history."""

    def __init__(
        self,
        *,
        base_url: str = "https://tonapi.io",
        api_key: str | None = None,
        timeout_seconds: int = 30,
        unauth_min_interval_seconds: float = 4.0,
    ) -> None:
        normalized_base = base_url.strip().rstrip("/")
        if not normalized_base:
            raise ValueError("base_url must not be empty")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        if unauth_min_interval_seconds < 0:
            raise ValueError("unauth_min_interval_seconds must be >= 0")

        self._base_url = normalized_base
        self._api_key = api_key.strip() if api_key is not None and api_key.strip() else None
        self._timeout_seconds = timeout_seconds
        self._unauth_min_interval_seconds = unauth_min_interval_seconds
        self._throttle_lock = threading.Lock()
        self._last_request_monotonic = 0.0

    async def parse_address(self, *, account_id: str) -> TonapiAddressInfo:
        normalized_account_id = account_id.strip()
        if not normalized_account_id:
            raise ValueError("account_id must not be empty")

        payload = await asyncio.to_thread(
            self._request_json,
            f"/v2/address/{urllib.parse.quote(normalized_account_id, safe='')}/parse",
            None,
        )
        raw_form = str(payload.get("raw_form", "")).strip()
        if not raw_form:
            raise TonapiApiError(
                status_code=None,
                message="address parse response missing raw_form",
            )
        return TonapiAddressInfo(raw_form=raw_form)

    async def get_jetton_account_history(
        self,
        *,
        account_id: str,
        jetton_id: str,
        limit: int,
        before_lt: int | None = None,
    ) -> TonapiJettonHistoryPage:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        normalized_account_id = account_id.strip()
        normalized_jetton_id = jetton_id.strip()
        if not normalized_account_id:
            raise ValueError("account_id must not be empty")
        if not normalized_jetton_id:
            raise ValueError("jetton_id must not be empty")

        query: dict[str, str] = {"limit": str(limit)}
        if before_lt is not None:
            query["before_lt"] = str(before_lt)

        path = (
            f"/v2/jettons/{urllib.parse.quote(normalized_jetton_id, safe='')}/"
            f"accounts/{urllib.parse.quote(normalized_account_id, safe='')}/history"
        )
        payload = await asyncio.to_thread(self._request_json, path, query)

        operations_payload = payload.get("operations")
        if not isinstance(operations_payload, list):
            operations_payload = []
        operations: list[TonapiJettonOperation] = []
        for row in operations_payload:
            if not isinstance(row, dict):
                continue
            operation = str(row.get("operation", "")).strip().lower()
            if operation not in {"transfer", "mint", "burn"}:
                continue
            utime_value = int(row.get("utime", 0))
            lt_value = int(row.get("lt", 0))
            tx_hash = str(row.get("transaction_hash", "")).strip()
            amount_raw = str(row.get("amount", "")).strip()
            query_id = str(row.get("query_id", "")).strip()
            trace_id = str(row.get("trace_id", "")).strip()
            if not tx_hash or not amount_raw or lt_value < 1 or not trace_id:
                continue

            source_address = _account_address_value(row.get("source"))
            destination_address = _account_address_value(row.get("destination"))

            jetton = row.get("jetton")
            decimals = 6
            if isinstance(jetton, dict):
                decimals_raw = jetton.get("decimals")
                if isinstance(decimals_raw, int):
                    decimals = decimals_raw
            operations.append(
                TonapiJettonOperation(
                    operation=operation,
                    utime=datetime.fromtimestamp(utime_value, tz=UTC),
                    lt=lt_value,
                    transaction_hash=tx_hash,
                    source_address=source_address,
                    destination_address=destination_address,
                    amount_raw=amount_raw,
                    decimals=decimals,
                    query_id=query_id,
                    trace_id=trace_id,
                    payload=row,
                )
            )

        next_from_raw = payload.get("next_from")
        next_from: int | None = None
        if next_from_raw is not None:
            try:
                next_from = int(next_from_raw)
            except (TypeError, ValueError):
                next_from = None
        return TonapiJettonHistoryPage(operations=operations, next_from=next_from)

    def _request_json(self, path: str, query: dict[str, str] | None) -> dict[str, Any]:
        self._maybe_rate_limit()

        query_string = urllib.parse.urlencode(query) if query else ""
        url = f"{self._base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(url, method="GET", headers=headers)

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload_text = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
                if not 200 <= status < 300:
                    raise TonapiApiError(
                        status_code=status,
                        message=_extract_error_message(payload_text) or "unexpected status",
                    )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TonapiApiError(
                status_code=exc.code,
                message=(_extract_error_message(body) or str(exc.reason))[:1000],
            ) from exc
        except urllib.error.URLError as exc:
            raise TonapiApiError(status_code=None, message=str(exc.reason)) from exc
        except TimeoutError as exc:
            raise TonapiApiError(status_code=None, message="timeout") from exc

        try:
            parsed = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise TonapiApiError(status_code=status, message="invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise TonapiApiError(status_code=status, message="unexpected response format")
        return parsed

    def _maybe_rate_limit(self) -> None:
        if self._api_key is not None:
            return
        if self._unauth_min_interval_seconds <= 0:
            return
        with self._throttle_lock:
            now = time.monotonic()
            wait_for = self._unauth_min_interval_seconds - (now - self._last_request_monotonic)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request_monotonic = time.monotonic()


def _account_address_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("address")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    return normalized or None


def _extract_error_message(payload: str) -> str | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    for key in ("error", "message", "detail", "title"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
