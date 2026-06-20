from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import Config


def create_bot(config: Config) -> Bot:
    session = AiohttpSession(proxy=config.telegram.PROXY_URL)
    return Bot(
        token=config.bot.TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
