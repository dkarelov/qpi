from __future__ import annotations

from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class DatabasePool:
    """Thin wrapper around psycopg async connection pooling."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int,
        max_size: int,
        statement_timeout_ms: int,
    ) -> None:
        self._statement_timeout_ms = statement_timeout_ms
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"autocommit": False, "row_factory": dict_row},
            configure=self._configure_connection,
        )

    async def _configure_connection(self, conn: AsyncConnection) -> None:
        async with conn.cursor() as cur:
            await cur.execute("SET TIME ZONE 'UTC'")
            await cur.execute("SET statement_timeout = %s", (self._statement_timeout_ms,))

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    async def check(self) -> None:
        async with self.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool

    @asynccontextmanager
    async def connection(self):
        async with self._pool.connection() as conn:
            yield conn
