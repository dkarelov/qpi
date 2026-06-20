from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.private.windows import Window
from app.bot.manager import Manager
from app.bot.support_context import parse_start_payload
from app.bot.utils.redis import RedisStorage
from app.bot.utils.redis.models import UserData
from app.config import Config

router = Router()
router.message.filter(F.chat.type == "private")


class IsDev(BaseFilter):
    """Allow only configured developer/admin IDs (``BOT_DEV_IDS``)."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return message.from_user is not None and message.from_user.id in config.bot.DEV_IDS


@router.message(Command("start"))
async def handler(
    message: Message,
    command: CommandObject,
    manager: Manager,
    redis: RedisStorage,
    user_data: UserData,
) -> None:
    """
    Handles the /start command.

    If the user has already selected a language, displays the main menu window.
    Otherwise, prompts the user to select a language.

    :param message: Message object.
    :param manager: Manager object.
    :param redis: RedisStorage object.
    :param user_data: UserData object.
    :return: None
    """
    user_data.set_support_context(parse_start_payload(command.args))
    await redis.update_user(user_data.id, user_data)

    if user_data.language_code:
        await Window.main_menu(manager)
    else:
        await Window.select_language(manager)
    await manager.delete_message(message)


@router.message(Command("language"))
async def handler(message: Message, manager: Manager, user_data: UserData) -> None:
    """
    Handles the /language command.

    If the user has already selected a language, prompts the user to select a new language.
    Otherwise, prompts the user to select a language.

    :param message: Message object.
    :param manager: Manager object.
    :param user_data: UserData object.
    :return: None
    """
    if user_data.language_code:
        await Window.change_language(manager)
    else:
        await Window.select_language(manager)
    await manager.delete_message(message)


@router.message(Command("newsletter"), IsDev())
async def handler(
    message: Message,
    manager: Manager,
) -> None:
    """
    Handles the /newsletter command — opens the broadcast UI for admins.

    :param message: Message object.
    :param manager: Manager object.
    :param broadcast_ui: Broadcast UI manager (from aiogram-broadcast).
    :param broadcast_storage: Broadcast subscriber storage.
    :return: None
    """
    await manager.send_message("Newsletter menu is disabled until newsletter configuration is enabled.")
    await manager.delete_message(message)
