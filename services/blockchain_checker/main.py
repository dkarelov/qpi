from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import asdict

from libs.config.settings import get_blockchain_checker_settings
from libs.db.pool import DatabasePool
from libs.domain.blockchain_checker import BlockchainCheckerRunResult, BlockchainCheckerService
from libs.domain.deposit_intents import DepositIntentService
from libs.integrations.tonapi import TonapiClient
from libs.logging.setup import configure_logging, get_logger


async def run_once(*, request_id: str | None = None) -> BlockchainCheckerRunResult:
    settings = get_blockchain_checker_settings()
    configure_logging("blockchain_checker", settings.log_level, request_id=request_id)
    logger = get_logger(__name__)
    started_at = time.monotonic()

    db_pool = DatabasePool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    await db_pool.open()
    logger.info(
        "blockchain_checker_started",
        env=settings.app_env,
        db_pool_min_size=settings.db_pool_min_size,
        db_pool_max_size=settings.db_pool_max_size,
        advisory_lock_id=settings.blockchain_checker_advisory_lock_id,
        match_batch_size=settings.blockchain_checker_match_batch_size,
        tonapi_base_url=settings.tonapi_base_url,
        tonapi_page_limit=settings.tonapi_page_limit,
        tonapi_max_pages_per_shard=settings.tonapi_max_pages_per_shard,
        confirmations_required=settings.blockchain_checker_confirmations_required,
        shard_key=settings.seller_collateral_shard_key,
    )

    try:
        await db_pool.check()
        logger.info("blockchain_checker_db_connectivity_ok")

        tonapi_client = TonapiClient(
            base_url=settings.tonapi_base_url,
            api_key=settings.tonapi_api_key,
            timeout_seconds=settings.tonapi_timeout_seconds,
            unauth_min_interval_seconds=settings.tonapi_unauth_min_interval_seconds,
        )
        deposit_service = DepositIntentService(
            db_pool.pool,
            invoice_ttl_hours=settings.seller_collateral_invoice_ttl_hours,
        )
        service = BlockchainCheckerService(
            db_pool.pool,
            advisory_lock_conninfo=settings.database_url,
            advisory_lock_id=settings.blockchain_checker_advisory_lock_id,
            shard_key=settings.seller_collateral_shard_key,
            shard_address=settings.seller_collateral_shard_address,
            shard_chain=settings.seller_collateral_shard_chain,
            shard_asset=settings.seller_collateral_shard_asset,
            usdt_jetton_master=settings.tonapi_usdt_jetton_master,
            page_limit=settings.tonapi_page_limit,
            max_pages_per_shard=settings.tonapi_max_pages_per_shard,
            match_batch_size=settings.blockchain_checker_match_batch_size,
            confirmations_required=settings.blockchain_checker_confirmations_required,
            tonapi_client=tonapi_client,
            deposit_service=deposit_service,
            logger=logger,
        )

        result = await service.run_once()
        summary = asdict(result)
        summary["duration_ms"] = int((time.monotonic() - started_at) * 1000)
        if not result.lock_acquired:
            logger.warning("blockchain_checker_finished_lock_not_acquired", **summary)
        else:
            logger.info("blockchain_checker_finished", **summary)
        return result
    except Exception as exc:
        logger.exception(
            "blockchain_checker_failed",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise
    finally:
        await db_pool.close()
        logger.info(
            "blockchain_checker_stopped",
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )


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
    parser = argparse.ArgumentParser(description="QPI blockchain checker")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run_service(run_once_mode=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
