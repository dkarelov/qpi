from __future__ import annotations

import argparse
import asyncio

from libs.config.settings import get_bot_api_settings
from libs.db.pool import DatabasePool
from libs.domain.buyer import BuyerService
from libs.domain.seller import SellerService
from libs.domain.seller_workflow import SellerWorkflowService
from libs.integrations.wb import WbPingClient
from libs.integrations.wb_public import WbPublicCatalogClient
from libs.logging.setup import configure_logging, get_logger
from services.bot_api.buyer_handlers import BuyerCommandProcessor
from services.bot_api.seller_handlers import SellerCommandProcessor


async def run_service(
    *,
    run_once: bool = False,
    seller_command: str | None = None,
    buyer_command: str | None = None,
    telegram_id: int = 0,
    telegram_username: str | None = None,
) -> None:
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

        if seller_command and buyer_command:
            raise ValueError("use only one of --seller-command or --buyer-command")

        if seller_command:
            seller_service = SellerService(db_pool.pool)
            wb_ping_client = WbPingClient(
                timeout_seconds=settings.wb_ping_timeout_seconds,
                max_requests=settings.wb_ping_rate_limit_count,
                window_seconds=settings.wb_ping_rate_limit_window_seconds,
            )
            seller_workflow_service = SellerWorkflowService(
                seller_service=seller_service,
                wb_public_client=WbPublicCatalogClient(
                    content_timeout_seconds=settings.wb_content_timeout_seconds,
                    orders_timeout_seconds=settings.wb_orders_timeout_seconds,
                    orders_lookback_days=settings.wb_orders_lookback_days,
                ),
                token_cipher_key=settings.token_cipher_key,
            )
            processor = SellerCommandProcessor(
                seller_service=seller_service,
                seller_workflow_service=seller_workflow_service,
                wb_ping_client=wb_ping_client,
                token_cipher_key=settings.token_cipher_key,
                bot_username=settings.telegram_bot_username,
            )
            response = await processor.handle(
                telegram_id=telegram_id,
                username=telegram_username,
                text=seller_command,
            )
            logger.info(
                "seller_command_processed",
                command=seller_command,
                telegram_id=telegram_id,
                delete_source_message=response.delete_source_message,
                response=response.text,
            )
            return

        if buyer_command:
            buyer_service = BuyerService(db_pool.pool)
            processor = BuyerCommandProcessor(
                buyer_service=buyer_service,
                bot_username=settings.telegram_bot_username,
            )
            response = await processor.handle(
                telegram_id=telegram_id,
                username=telegram_username,
                text=buyer_command,
            )
            logger.info(
                "buyer_command_processed",
                command=buyer_command,
                telegram_id=telegram_id,
                delete_source_message=response.delete_source_message,
                response=response.text,
            )
            return

        if run_once:
            return

        raise ValueError(
            "webhook mode is not available in async command runner; "
            "run CLI without --once/--seller-command/--buyer-command."
        )
    finally:
        await db_pool.close()
        logger.info("bot_api_stopped")


def run_webhook_runtime() -> None:
    settings = get_bot_api_settings()
    configure_logging("bot_api", settings.log_level)
    logger = get_logger(__name__)
    logger.info("bot_api_webhook_mode_selected")
    from services.bot_api.telegram_runtime import TelegramWebhookRuntime

    runtime = TelegramWebhookRuntime(settings=settings, logger=logger)
    runtime.run()


def cli() -> None:
    parser = argparse.ArgumentParser(description="QPI bot API service")
    parser.add_argument("--once", action="store_true", help="start, run checks, and exit")
    parser.add_argument(
        "--seller-command",
        default=None,
        help="process one seller command and exit",
    )
    parser.add_argument(
        "--buyer-command",
        default=None,
        help="process one buyer command and exit",
    )
    parser.add_argument(
        "--telegram-id",
        type=int,
        default=0,
        help="telegram id used with --seller-command",
    )
    parser.add_argument(
        "--telegram-username",
        default=None,
        help="telegram username used with --seller-command",
    )
    args = parser.parse_args()

    try:
        if args.once or args.seller_command or args.buyer_command:
            asyncio.run(
                run_service(
                    run_once=args.once,
                    seller_command=args.seller_command,
                    buyer_command=args.buyer_command,
                    telegram_id=args.telegram_id,
                    telegram_username=args.telegram_username,
                )
            )
        else:
            run_webhook_runtime()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
