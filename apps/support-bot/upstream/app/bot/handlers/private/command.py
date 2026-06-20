from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.private.windows import Window
from app.bot.manager import Manager
from app.bot.support_runtime import account_from_user_data, build_support_topic_service
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
async def start_handler(
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
    user_data.language_code = "ru"
    await redis.update_user(user_data.id, user_data)
    service = build_support_topic_service(message.bot, redis, manager.config, current_user=user_data)
    await service.record_start_payload(account_from_user_data(user_data), command.args)
    await Window.main_menu(manager)
    await manager.delete_message(message)


@router.message(Command("language"))
async def language_handler(message: Message, manager: Manager, redis: RedisStorage, user_data: UserData) -> None:
    """
    Handles the /language command.

    qpi support-bot end-user UX is Russian-only, so this command confirms the
    fixed language instead of opening a selector.

    :param message: Message object.
    :param manager: Manager object.
    :param user_data: UserData object.
    :return: None
    """
    user_data.language_code = "ru"
    await redis.update_user(user_data.id, user_data)
    await manager.send_message(manager.text_message.get("language_fixed"))
    await manager.delete_message(message)


@router.message(Command("newsletter"), IsDev())
async def newsletter_handler(
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
