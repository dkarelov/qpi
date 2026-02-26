from __future__ import annotations

import argparse
import asyncio

from libs.config.settings import get_worker_settings
from libs.db.pool import DatabasePool
from libs.logging.setup import configure_logging, get_logger


async def run_service(run_once: bool = False) -> None:
    settings = get_worker_settings()
    configure_logging("worker", settings.log_level)
    logger = get_logger(__name__)

    db_pool = DatabasePool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    await db_pool.open()
    logger.info("worker_started", env=settings.app_env)

    try:
        await db_pool.check()
        logger.info("db_connectivity_ok")

        async def run_tick() -> None:
            logger.info(
                "worker_tick_noop",
                reason="phase6_order_tracker_owns_reservation_expiry",
            )

        if run_once:
            await run_tick()
            return

        while True:
            await run_tick()
            await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await db_pool.close()
        logger.info("worker_stopped")


def cli() -> None:
    parser = argparse.ArgumentParser(description="QPI worker service")
    parser.add_argument("--once", action="store_true", help="start, run checks, and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run_service(run_once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
