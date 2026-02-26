from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict

from libs.config.settings import get_order_tracker_settings
from libs.db.pool import DatabasePool
from libs.domain.order_tracker import OrderTrackerRunResult, OrderTrackerService
from libs.logging.setup import configure_logging, get_logger


async def run_once(*, request_id: str | None = None) -> OrderTrackerRunResult:
    settings = get_order_tracker_settings()
    configure_logging("order_tracker", settings.log_level, request_id=request_id)
    logger = get_logger(__name__)

    db_pool = DatabasePool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    await db_pool.open()
    logger.info("order_tracker_started", env=settings.app_env)

    try:
        await db_pool.check()
        logger.info("db_connectivity_ok")

        service = OrderTrackerService(
            db_pool.pool,
            advisory_lock_conninfo=settings.database_url,
            advisory_lock_id=settings.order_tracker_advisory_lock_id,
            reservation_expiry_batch_size=settings.order_tracker_reservation_expiry_batch_size,
            wb_event_batch_size=settings.order_tracker_wb_event_batch_size,
            delivery_expiry_batch_size=settings.order_tracker_delivery_expiry_batch_size,
            unlock_batch_size=settings.order_tracker_unlock_batch_size,
            delivery_expiry_days=settings.order_tracker_delivery_expiry_days,
            unlock_days=settings.order_tracker_unlock_days,
        )
        result = await service.run_once()
        logger.info("order_tracker_finished", **asdict(result))
        return result
    finally:
        await db_pool.close()
        logger.info("order_tracker_stopped")


def handler(event, context):
    request_id = getattr(context, "request_id", None)
    result = asyncio.run(run_once(request_id=request_id))
    payload = asdict(result)
    payload["ok"] = True
    return payload


async def run_service(*, run_once_mode: bool = False) -> None:
    await run_once()
    if run_once_mode:
        return

    while True:
        await asyncio.sleep(300)
        await run_once()


def cli() -> None:
    parser = argparse.ArgumentParser(description="QPI order tracker")
    parser.add_argument("--once", action="store_true", help="run one orchestration cycle and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run_service(run_once_mode=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
