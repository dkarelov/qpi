from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, MagicData
from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode

from app.bot.manager import Manager
from app.bot.support_runtime import build_support_topic_service
from app.bot.utils.redis import RedisStorage

router_id = Router()
router_id.message.filter(
    F.chat.type.in_(["group", "supergroup"]),
)


@router_id.message(Command("id"))
async def handler(message: Message) -> None:
    """
    Sends chat ID in response to the /id command.

    :param message: Message object.
    :return: None
    """
    await message.reply(hcode(message.chat.id))


router = Router()
router.message.filter(
    F.message_thread_id.is_not(None),
    F.chat.type.in_(["group", "supergroup"]),
    MagicData(F.event_chat.id == F.config.bot.GROUP_ID),  # type: ignore
)


@router.message(Command("silent"))
async def handler(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """
    Toggles silent mode for a user in the group.
    If silent mode is disabled, it will be enabled, and vice versa.

    :param message: Message object.
    :param manager: Manager object.
    :param redis: RedisStorage object.
    :return: None
    """
    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return None  # noqa

    service = build_support_topic_service(message.bot, redis, manager.config, current_user=user_data)
    thread_id = message.message_thread_id
    assert thread_id is not None

    if user_data.message_silent_mode:
        text = manager.text_message.get("silent_mode_disabled")
        with suppress(TelegramBadRequest):
            await message.reply(text)
        await service.set_silent(thread_id=thread_id, is_silent=False)
    else:
        text = manager.text_message.get("silent_mode_enabled")
        with suppress(TelegramBadRequest):
            await message.reply(text)
        await service.set_silent(thread_id=thread_id, is_silent=True)


@router.message(Command("information"))
async def handler(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """
    Sends user information in response to the /information command.

    :param message: Message object.
    :param manager: Manager object.
    :param redis: RedisStorage object.
    :return: None
    """
    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return None  # noqa

    format_data = user_data.to_dict()
    format_data["full_name"] = hbold(format_data["full_name"])
    text = manager.text_message.get("user_information")
    # Reply with formatted user information
    await message.reply(text.format_map(format_data))


@router.message(Command(commands=["ban"]))
async def handler(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """
    Toggles the ban status for a user in the group.
    If the user is banned, they will be unbanned, and vice versa.

    :param message: Message object.
    :param manager: Manager object.
    :param redis: RedisStorage object.
    :return: None
    """
    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return None  # noqa

    service = build_support_topic_service(message.bot, redis, manager.config, current_user=user_data)
    thread_id = message.message_thread_id
    assert thread_id is not None

    if user_data.is_banned:
        await service.set_banned(thread_id=thread_id, is_banned=False)
        text = manager.text_message.get("user_unblocked")
    else:
        await service.set_banned(thread_id=thread_id, is_banned=True)
        text = manager.text_message.get("user_blocked")

    # Reply with the specified text
    await message.reply(text)
