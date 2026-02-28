from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.integrations.fx_rates import FxRateProvider, FxRateProviderError
from libs.logging.setup import EventLogger, get_logger

_RATE_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class FxRateRow:
    pair_code: str
    rate: Decimal
    source: str
    fetched_at: datetime


class FxRateService:
    """Lazy-refresh FX cache stored in PostgreSQL."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        provider: FxRateProvider,
        refresh_lock_id: int = 85001,
        pair_code: str = "USDT_RUB",
        logger: EventLogger | None = None,
    ) -> None:
        if refresh_lock_id < 1:
            raise ValueError("refresh_lock_id must be >= 1")
        normalized_pair = pair_code.strip().upper()
        if not normalized_pair:
            raise ValueError("pair_code must not be empty")
        self._pool = pool
        self._provider = provider
        self._refresh_lock_id = refresh_lock_id
        self._pair_code = normalized_pair
        self._logger = logger or get_logger(__name__)

    async def get_usdt_rub_rate(
        self,
        *,
        max_age_seconds: int,
        fallback_rate: Decimal,
    ) -> Decimal:
        if max_age_seconds < 1:
            raise ValueError("max_age_seconds must be >= 1")
        fallback = _normalize_rate(fallback_rate)
        fresh_since = datetime.now(UTC) - timedelta(seconds=max_age_seconds)

        row = await self._get_rate_row()
        if row is not None and row.fetched_at >= fresh_since:
            return row.rate

        refreshed = await self._try_refresh_rate(fresh_since=fresh_since)
        if refreshed is not None:
            return refreshed

        latest = await self._get_rate_row()
        if latest is not None:
            return latest.rate
        return fallback

    async def _try_refresh_rate(self, *, fresh_since: datetime) -> Decimal | None:
        async with self._pool.connection() as conn:
            acquired = await self._try_advisory_lock(conn)
            if not acquired:
                return None
            try:
                row = await self._get_rate_row()
                if row is not None and row.fetched_at >= fresh_since:
                    return row.rate

                try:
                    fetched = await self._provider.fetch_usdt_rub_rate()
                except FxRateProviderError as exc:
                    self._logger.warning(
                        "fx_rate_fetch_failed",
                        source=self._provider.source_name,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:300],
                    )
                    return None
                except Exception as exc:
                    self._logger.warning(
                        "fx_rate_fetch_failed",
                        source=self._provider.source_name,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:300],
                    )
                    return None

                normalized = _normalize_rate(fetched)
                fetched_at = datetime.now(UTC)
                await self._upsert_rate_row(
                    pair_code=self._pair_code,
                    rate=normalized,
                    source=self._provider.source_name,
                    fetched_at=fetched_at,
                )
                self._logger.info(
                    "fx_rate_refreshed",
                    pair_code=self._pair_code,
                    rate=str(normalized),
                    source=self._provider.source_name,
                    fetched_at=fetched_at.isoformat(),
                )
                return normalized
            finally:
                await self._advisory_unlock(conn)

    async def _try_advisory_lock(self, conn: AsyncConnection) -> bool:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT pg_try_advisory_lock(%s) AS acquired
                """,
                (self._refresh_lock_id,),
            )
            row = await cur.fetchone()
            return bool(row["acquired"])

    async def _advisory_unlock(self, conn: AsyncConnection) -> None:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT pg_advisory_unlock(%s) AS unlocked
                    """,
                    (self._refresh_lock_id,),
                )
                await cur.fetchone()
        except Exception as exc:
            self._logger.warning(
                "fx_rate_unlock_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )

    async def _get_rate_row(self) -> FxRateRow | None:
        async def operation(conn: AsyncConnection) -> FxRateRow | None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT pair_code, rate, source, fetched_at
                    FROM fx_rates
                    WHERE pair_code = %s
                    """,
                    (self._pair_code,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return FxRateRow(
                    pair_code=row["pair_code"],
                    rate=_normalize_rate(row["rate"]),
                    source=row["source"],
                    fetched_at=row["fetched_at"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def _upsert_rate_row(
        self,
        *,
        pair_code: str,
        rate: Decimal,
        source: str,
        fetched_at: datetime,
    ) -> None:
        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO fx_rates (
                        pair_code,
                        rate,
                        source,
                        fetched_at
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (pair_code)
                    DO UPDATE SET
                        rate = EXCLUDED.rate,
                        source = EXCLUDED.source,
                        fetched_at = EXCLUDED.fetched_at,
                        updated_at = timezone('utc', now())
                    """,
                    (pair_code, rate, source, fetched_at),
                )

        await run_in_transaction(self._pool, operation)


def _normalize_rate(value: Decimal) -> Decimal:
    rate = Decimal(value).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)
    if rate <= Decimal("0"):
        raise ValueError("rate must be > 0")
    return rate
