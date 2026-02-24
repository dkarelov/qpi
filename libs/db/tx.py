from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from psycopg import AsyncConnection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import AsyncConnectionPool

T = TypeVar("T")


async def run_in_transaction(
    pool: AsyncConnectionPool,
    operation: Callable[[AsyncConnection], Awaitable[T]],
    *,
    read_only: bool = False,
    max_retries: int = 3,
) -> T:
    """Run operation in serializable transaction with retry on retryable errors."""

    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    attempt = 1
    while True:
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute("SET LOCAL TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                        if read_only:
                            await cur.execute("SET LOCAL TRANSACTION READ ONLY")
                    return await operation(conn)
        except (SerializationFailure, DeadlockDetected):
            if attempt >= max_retries:
                raise
            await asyncio.sleep(0.05 * attempt)
            attempt += 1
