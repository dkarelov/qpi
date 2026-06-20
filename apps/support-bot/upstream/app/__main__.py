import asyncio
import logging

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from .bot import commands
from .bot.handlers import include_routers
from .bot.llm import get_provider
from .bot.middlewares import register_middlewares
from .bot.policy import load_policy
from .bot.storage import create_schema
from .bot.telegram_client import create_bot
from .config import Config, load_config
from .logger import setup_logger


async def on_shutdown(
    dispatcher: Dispatcher,
    config: Config,
    bot: Bot,
    pg_pool: asyncpg.Pool,
) -> None:
    """
    Shutdown event handler. This runs when the bot shuts down.

    :param apscheduler: AsyncIOScheduler: The apscheduler instance.
    :param dispatcher: Dispatcher: The bot dispatcher.
    :param config: Config: The config instance.
    :param bot: Bot: The bot instance.
    :param pg_pool: asyncpg.Pool: The PostgreSQL connection pool.
    """
    await commands.delete(bot, config)
    await dispatcher.storage.close()
    await pg_pool.close()
    await bot.delete_webhook()
    await bot.session.close()


async def on_startup(
    config: Config,
    bot: Bot,
) -> None:
    """
    Startup event handler. This runs when the bot starts up.

    :param apscheduler: AsyncIOScheduler: The apscheduler instance.
    :param config: Config: The config instance.
    :param bot: Bot: The bot instance.
    """
    await commands.setup(bot, config)


async def main() -> None:
    """
    Main function that initializes the bot and starts the event loop.
    """
    # Load config
    config = load_config()

    storage = RedisStorage.from_url(
        url=config.redis.dsn(),
    )

    pg_pool = await asyncpg.create_pool(config.db.URL)
    await create_schema(pg_pool, schema=config.db.SCHEMA)

    bot = create_bot(config)

    dp = Dispatcher(
        storage=storage,
        config=config,
        bot=bot,
    )
    dp["pg_pool"] = pg_pool

    # Optional policy engine and LLM provider (both disabled by default).
    # Exposed as workflow data so aiogram injects them into handlers as kwargs.
    # A bad/missing policy config must never crash the bot — log and continue.
    policy_engine = None
    if config.policy.ENABLED:
        try:
            policy_engine = load_policy(config.policy)
        except Exception as ex:  # noqa: BLE001
            logging.error("Failed to load policy; continuing without it: %s", ex)
    dp["policy_engine"] = policy_engine
    dp["llm_provider"] = get_provider(config.ai)

    # Register startup handler
    dp.startup.register(on_startup)
    # Register shutdown handler
    dp.shutdown.register(on_shutdown)

    # Include routes
    include_routers(dp)
    # Register middlewares
    register_middlewares(dp, pool=pg_pool, schema=config.db.SCHEMA)

    # Start the bot. Keep pending updates so messages sent while the bot was
    # offline (e.g. during a redeploy) are processed, not dropped.
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def cli() -> None:
    setup_logger()
    asyncio.run(main())


if __name__ == "__main__":
    cli()
