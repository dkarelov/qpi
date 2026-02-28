from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from libs.domain.fx_rates import FxRateService
from libs.integrations.fx_rates import FxRateProviderError


class StubFxProvider:
    source_name = "stub"

    def __init__(self, *, rate: Decimal | None = None, error: Exception | None = None) -> None:
        self._rate = rate
        self._error = error
        self.calls = 0

    async def fetch_usdt_rub_rate(self) -> Decimal:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._rate is None:
            raise FxRateProviderError("missing rate")
        return self._rate


@pytest.mark.asyncio
async def test_fx_rate_refreshes_and_is_cached(db_pool) -> None:
    provider = StubFxProvider(rate=Decimal("101.25"))
    service = FxRateService(db_pool, provider=provider, refresh_lock_id=85101)

    first = await service.get_usdt_rub_rate(
        max_age_seconds=900,
        fallback_rate=Decimal("90"),
    )
    second = await service.get_usdt_rub_rate(
        max_age_seconds=900,
        fallback_rate=Decimal("90"),
    )

    assert first == Decimal("101.250000")
    assert second == Decimal("101.250000")
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_fx_rate_returns_stale_value_when_provider_fails(db_pool) -> None:
    stale_rate = Decimal("95.5")
    fetched_at = datetime.now(UTC) - timedelta(days=1)
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO fx_rates (pair_code, rate, source, fetched_at)
                    VALUES ('USDT_RUB', %s, 'seed', %s)
                    """,
                    (stale_rate, fetched_at),
                )

    provider = StubFxProvider(error=FxRateProviderError("provider down"))
    service = FxRateService(db_pool, provider=provider, refresh_lock_id=85102)

    value = await service.get_usdt_rub_rate(
        max_age_seconds=900,
        fallback_rate=Decimal("90"),
    )

    assert value == Decimal("95.500000")
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_fx_rate_returns_fallback_when_no_cache_and_provider_fails(db_pool) -> None:
    provider = StubFxProvider(error=FxRateProviderError("provider down"))
    service = FxRateService(db_pool, provider=provider, refresh_lock_id=85103)

    value = await service.get_usdt_rub_rate(
        max_age_seconds=900,
        fallback_rate=Decimal("90"),
    )

    assert value == Decimal("90.000000")
    assert provider.calls == 1
