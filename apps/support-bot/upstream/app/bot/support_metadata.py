from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.bot.storage import UserData
from app.bot.support_context import render_pinned_metadata
from app.bot.support_topics import SupportTopic, TelegramAccount
from app.config import Config


async def pin_support_metadata(bot: Bot, config: Config, user_data: UserData) -> None:
    if user_data.message_thread_id is None:
        return
    username = None if user_data.username == "-" else user_data.username
    account = TelegramAccount(id=user_data.id, full_name=user_data.full_name, username=username)
    topic = SupportTopic(
        telegram_id=user_data.id,
        thread_id=user_data.message_thread_id,
        title="",
        context=user_data.support_context(),
        status=user_data.status,
        is_banned=user_data.is_banned,
        is_silent=user_data.message_silent_mode,
        full_name=user_data.full_name,
        username=username,
    )
    with suppress(TelegramBadRequest):
        message = await bot.send_message(
            chat_id=config.bot.GROUP_ID,
            text=render_pinned_metadata(account, topic),
            message_thread_id=user_data.message_thread_id,
        )
        await message.pin(disable_notification=True)
