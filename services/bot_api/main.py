from __future__ import annotations

import argparse
import asyncio

from libs.config.settings import get_bot_api_settings
from libs.db.pool import DatabasePool
from libs.logging.setup import configure_logging, get_logger


async def run_service(run_once: bool = False) -> None:
    settings = get_bot_api_settings()
    configure_logging("bot_api", settings.log_level)
    logger = get_logger(__name__)

    db_pool = DatabasePool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    await db_pool.open()
    logger.info("bot_api_started", env=settings.app_env)

    try:
        await db_pool.check()
        logger.info("db_connectivity_ok")

        if run_once:
            return

        while True:
            await asyncio.sleep(60)
    finally:
        await db_pool.close()
        logger.info("bot_api_stopped")


def cli() -> None:
    parser = argparse.ArgumentParser(description="QPI bot API service")
    parser.add_argument("--once", action="store_true", help="start, run checks, and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run_service(run_once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
