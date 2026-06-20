from __future__ import annotations

import asyncio
import os

import asyncpg

from app.bot.storage import create_schema


async def main() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=1)
    try:
        await create_schema(pool, os.environ.get("SUPPORT_BOT_DB_SCHEMA", "support_bot"))
    finally:
        await pool.close()
    print("support_bot_postgres_ok=true")


if __name__ == "__main__":
    asyncio.run(main())
