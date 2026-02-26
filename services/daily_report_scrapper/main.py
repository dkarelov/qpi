from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict

from libs.config.settings import get_daily_report_scrapper_settings
from libs.db.pool import DatabasePool
from libs.domain.daily_report import DailyReportRunResult, DailyReportScrapperService
from libs.integrations.wb_reports import WbReportClient
from libs.logging.setup import configure_logging, get_logger


async def run_once(*, request_id: str | None = None) -> DailyReportRunResult:
    settings = get_daily_report_scrapper_settings()
    configure_logging("daily_report_scrapper", settings.log_level, request_id=request_id)
    logger = get_logger(__name__)

    db_pool = DatabasePool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    await db_pool.open()
    logger.info("daily_report_scrapper_started", env=settings.app_env)

    try:
        await db_pool.check()
        logger.info("db_connectivity_ok")

        service = DailyReportScrapperService(
            db_pool.pool,
            token_cipher_key=settings.token_cipher_key,
            wb_client=WbReportClient(
                endpoint=settings.wb_report_api_url,
                timeout_seconds=settings.wb_report_timeout_seconds,
            ),
            concurrency=settings.wb_report_concurrency,
            request_limit=settings.wb_report_limit,
            max_retries=settings.wb_report_max_retries,
            retry_delay_seconds=settings.wb_report_retry_delay_seconds,
            days_back=settings.wb_report_days_back,
        )
        result = await service.run_once()
        logger.info("daily_report_scrapper_finished", **asdict(result))
        return result
    finally:
        await db_pool.close()
        logger.info("daily_report_scrapper_stopped")


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
        await asyncio.sleep(3600)
        await run_once()


def cli() -> None:
    parser = argparse.ArgumentParser(description="QPI daily report scrapper")
    parser.add_argument("--once", action="store_true", help="run one sync cycle and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run_service(run_once_mode=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
