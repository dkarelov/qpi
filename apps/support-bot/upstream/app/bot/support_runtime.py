from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from app.bot.storage import RedisStorage, UserData
from app.bot.support_topics import MediaItem, SupportTopic, SupportTopicService, TelegramAccount
from app.bot.utils.exceptions import CreateForumTopicException, NotAForumException, NotEnoughRightsException

if TYPE_CHECKING:
    from app.bot.support_context import SupportContext
    from app.config import Config

logger = logging.getLogger(__name__)


def account_from_user_data(user_data: UserData) -> TelegramAccount:
    username = None if user_data.username in (None, "-") else user_data.username
    return TelegramAccount(id=user_data.id, full_name=user_data.full_name, username=username)


class PostgresSupportTopicStore:
    """SupportTopicStore adapter over the existing PostgreSQL-backed user repository."""

    def __init__(self, storage: RedisStorage, *, current_user: UserData | None = None) -> None:
        self.storage = storage
        self.current_user = current_user

    async def get_context(self, telegram_id: int) -> SupportContext:
        user = await self._get_user(telegram_id)
        if user is None:
            from app.bot.support_context import GENERIC_CONTEXT

            return GENERIC_CONTEXT
        return user.support_context()

    async def save_context(self, telegram_id: int, context: SupportContext) -> None:
        user = await self._get_user(telegram_id)
        if user is None:
            return
        user.set_support_context(context)
        await self.storage.update_user(user.id, user)

    async def get_by_telegram_id(self, telegram_id: int) -> SupportTopic | None:
        user = await self._get_user(telegram_id)
        return None if user is None else self._topic_from_user(user)

    async def get_by_thread_id(self, thread_id: int) -> SupportTopic | None:
        if self.current_user is not None and self.current_user.message_thread_id == thread_id:
            return self._topic_from_user(self.current_user)
        user = await self.storage.get_by_message_thread_id(thread_id)
        return None if user is None else self._topic_from_user(user)

    async def save(self, topic: SupportTopic) -> None:
        user = await self._get_user(topic.telegram_id)
        if user is None:
            user = UserData(
                message_thread_id=topic.thread_id,
                message_silent_id=None,
                message_silent_mode=topic.is_silent,
                id=topic.telegram_id,
                full_name=topic.full_name,
                username=topic.username or "-",
                is_banned=topic.is_banned,
                status=topic.status,
            )
        user.message_thread_id = topic.thread_id
        user.full_name = topic.full_name
        user.username = topic.username or "-"
        user.is_banned = topic.is_banned
        user.message_silent_mode = topic.is_silent
        user.message_silent_id = None
        user.status = topic.status
        user.set_support_context(topic.context)
        await self.storage.update_user(user.id, user)
        self.current_user = user

    async def _get_user(self, telegram_id: int) -> UserData | None:
        if self.current_user is not None and self.current_user.id == telegram_id:
            return self.current_user
        get_user = getattr(self.storage, "get_user", None)
        if get_user is None:
            return None
        return await get_user(telegram_id)

    @staticmethod
    def _topic_from_user(user: UserData) -> SupportTopic | None:
        if user.message_thread_id is None:
            return None
        username = None if user.username in (None, "-") else user.username
        return SupportTopic(
            telegram_id=user.id,
            thread_id=user.message_thread_id,
            title=user.full_name,
            context=user.support_context(),
            status=user.status,
            is_banned=user.is_banned,
            is_silent=user.message_silent_mode,
            full_name=user.full_name,
            username=username,
        )


class AiogramSupportTopicTelegram:
    """SupportTopicTelegram adapter for live aiogram Bot API operations."""

    def __init__(self, bot: Bot, config: Config, *, reply_message: Message | None = None) -> None:
        self.bot = bot
        self.config = config
        self.reply_message = reply_message

    async def create_topic(self, *, group_id: int, title: str) -> int:
        try:
            topic = await self.bot.create_forum_topic(
                chat_id=group_id,
                name=title,
                icon_custom_emoji_id=self.config.bot.BOT_EMOJI_ID,
                request_timeout=30,
            )
            return topic.message_thread_id
        except TelegramRetryAfter as ex:
            logger.warning("Support topic create rate-limited; retry_after=%s", ex.retry_after)
            await asyncio.sleep(ex.retry_after)
            return await self.create_topic(group_id=group_id, title=title)
        except TelegramBadRequest as ex:
            mapped = _map_create_topic_error(ex)
            await self._notify_create_failure(mapped)
            raise mapped from ex
        except Exception as ex:
            await self._notify_create_failure(ex)
            raise

    async def reopen_topic(self, *, group_id: int, thread_id: int) -> None:
        await self.bot.reopen_forum_topic(chat_id=group_id, message_thread_id=thread_id)

    async def close_topic(self, *, group_id: int, thread_id: int) -> None:
        await self.bot.close_forum_topic(chat_id=group_id, message_thread_id=thread_id)

    async def edit_topic_title(self, *, group_id: int, thread_id: int, title: str) -> None:
        await self.bot.edit_forum_topic(chat_id=group_id, message_thread_id=thread_id, name=title)

    async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=group_id, message_thread_id=thread_id, text=text)

    async def send_topic_media(self, *, group_id: int, thread_id: int, media: MediaItem) -> None:
        method = _media_send_method(self.bot, media.kind, private=False)
        await method(chat_id=group_id, message_thread_id=thread_id, **_media_kwargs(media))

    async def send_topic_album(self, *, group_id: int, thread_id: int, media: tuple[MediaItem, ...]) -> None:
        await self.bot.send_media_group(
            chat_id=group_id,
            message_thread_id=thread_id,
            media=[_input_media(item) for item in media],
        )

    async def send_private_text(self, *, telegram_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=telegram_id, text=text)

    async def send_private_media(self, *, telegram_id: int, media: MediaItem) -> None:
        method = _media_send_method(self.bot, media.kind, private=True)
        await method(chat_id=telegram_id, **_media_kwargs(media))

    async def send_private_album(self, *, telegram_id: int, media: tuple[MediaItem, ...]) -> None:
        await self.bot.send_media_group(chat_id=telegram_id, media=[_input_media(item) for item in media])

    async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
        if self.reply_message is not None:
            message = await self.reply_message.reply(text)
        else:
            message = await self.bot.send_message(chat_id=telegram_id, text=text)
        await asyncio.sleep(ttl_seconds)
        with suppress(TelegramBadRequest):
            await message.delete()

    async def send_user_failure(self, *, telegram_id: int, text: str, persistent: bool) -> None:
        if self.reply_message is not None:
            await self.reply_message.reply(text)
            return
        await self.bot.send_message(chat_id=telegram_id, text=text)

    async def notify_developer(self, *, text: str) -> None:
        await self.bot.send_message(chat_id=self.config.bot.DEV_ID, text=text)

    async def _notify_create_failure(self, ex: Exception) -> None:
        with suppress(Exception):
            await self.notify_developer(text=str(ex))


def build_support_topic_service(
    bot: Bot,
    storage: RedisStorage,
    config: Config,
    *,
    reply_message: Message | None = None,
    current_user: UserData | None = None,
) -> SupportTopicService:
    return SupportTopicService(
        store=PostgresSupportTopicStore(storage, current_user=current_user),
        telegram=AiogramSupportTopicTelegram(bot, config, reply_message=reply_message),
        group_id=config.bot.GROUP_ID,
    )


def _map_create_topic_error(ex: TelegramBadRequest) -> Exception:
    message = ex.message.lower()
    if "not enough rights" in message:
        return NotEnoughRightsException()
    if "not a forum" in message:
        return NotAForumException()
    return CreateForumTopicException()


def _media_kwargs(media: MediaItem) -> dict[str, Any]:
    return {media.kind: media.file_id, "caption": media.caption}


def _media_send_method(bot: Bot, kind: str, *, private: bool) -> Any:
    match kind:
        case "photo":
            return bot.send_photo
        case "video":
            return bot.send_video
        case "audio":
            return bot.send_audio
        case "document":
            return bot.send_document
        case _:
            location = "private" if private else "topic"
            raise ValueError(f"Unsupported {location} media kind: {kind}")


def _input_media(media: MediaItem) -> InputMediaPhoto | InputMediaVideo | InputMediaAudio | InputMediaDocument:
    match media.kind:
        case "photo":
            return InputMediaPhoto(media=media.file_id, caption=media.caption)
        case "video":
            return InputMediaVideo(media=media.file_id, caption=media.caption)
        case "audio":
            return InputMediaAudio(media=media.file_id, caption=media.caption)
        case "document":
            return InputMediaDocument(media=media.file_id, caption=media.caption)
        case _:
            raise ValueError(f"Unsupported album media kind: {media.kind}")
