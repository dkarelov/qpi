from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Protocol


class FxRateProviderError(Exception):
    """Raised when external FX provider request fails."""


class FxRateProvider(Protocol):
    """Provider contract for retrieving USDT/RUB rate."""

    source_name: str

    async def fetch_usdt_rub_rate(self) -> Decimal:
        """Return current USDT/RUB rate as positive Decimal."""


class CoinGeckoUsdtRubClient:
    """Minimal async CoinGecko simple-price client for USDT/RUB."""

    source_name = "coingecko_simple_price"

    def __init__(
        self,
        *,
        endpoint: str = "https://api.coingecko.com/api/v3/simple/price",
        timeout_seconds: int = 5,
        coin_id: str = "tether",
        vs_currency: str = "rub",
    ) -> None:
        normalized_endpoint = endpoint.strip()
        if not normalized_endpoint:
            raise ValueError("endpoint must not be empty")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        self._endpoint = normalized_endpoint
        self._timeout_seconds = timeout_seconds
        self._coin_id = coin_id.strip()
        self._vs_currency = vs_currency.strip()
        if not self._coin_id:
            raise ValueError("coin_id must not be empty")
        if not self._vs_currency:
            raise ValueError("vs_currency must not be empty")

    async def fetch_usdt_rub_rate(self) -> Decimal:
        return await asyncio.to_thread(self._fetch_usdt_rub_rate_sync)

    def _fetch_usdt_rub_rate_sync(self) -> Decimal:
        query = urllib.parse.urlencode(
            {
                "ids": self._coin_id,
                "vs_currencies": self._vs_currency,
            }
        )
        request = urllib.request.Request(
            f"{self._endpoint}?{query}",
            method="GET",
            headers={"Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                status = response.getcode()
                payload = response.read().decode("utf-8", errors="replace")
                if not 200 <= status < 300:
                    raise FxRateProviderError(f"unexpected status={status}")
        except urllib.error.HTTPError as exc:
            raise FxRateProviderError(f"http status={exc.code}") from exc
        except urllib.error.URLError as exc:
            raise FxRateProviderError(f"network error={exc.reason}") from exc
        except TimeoutError as exc:
            raise FxRateProviderError("timeout") from exc

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FxRateProviderError("invalid json response") from exc
        if not isinstance(parsed, dict):
            raise FxRateProviderError("unexpected payload shape")

        row = parsed.get(self._coin_id)
        if not isinstance(row, dict):
            raise FxRateProviderError("missing coin object in payload")
        raw_rate = row.get(self._vs_currency)
        if not isinstance(raw_rate, (int, float, str)):
            raise FxRateProviderError("missing rate in payload")

        try:
            rate = Decimal(str(raw_rate))
        except Exception as exc:
            raise FxRateProviderError("invalid rate value") from exc
        if rate <= Decimal("0"):
            raise FxRateProviderError("rate must be > 0")
        return rate
