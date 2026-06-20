from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.bot.support_context import (
    GENERIC_CONTEXT,
    SupportContext,
    parse_start_payload,
    render_pinned_metadata,
    render_topic_title,
)

USER_DELIVERY_ACK = "Сообщение отправлено в поддержку. Ответим здесь."
USER_DELIVERY_FAILURE = "Не удалось отправить сообщение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут."


@dataclass(frozen=True)
class TelegramAccount:
    id: int
    full_name: str
    username: str | None = None


@dataclass
class SupportTopic:
    telegram_id: int
    thread_id: int
    title: str
    context: SupportContext = GENERIC_CONTEXT
    status: str = "open"
    is_banned: bool = False


class SupportTopicStore(Protocol):
    async def get_context(self, telegram_id: int) -> SupportContext: ...

    async def save_context(self, telegram_id: int, context: SupportContext) -> None: ...

    async def get_by_telegram_id(self, telegram_id: int) -> SupportTopic | None: ...

    async def get_by_thread_id(self, thread_id: int) -> SupportTopic | None: ...

    async def save(self, topic: SupportTopic) -> None: ...


class SupportTopicTelegram(Protocol):
    async def create_topic(self, *, group_id: int, title: str) -> int: ...

    async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None: ...

    async def send_private_text(self, *, telegram_id: int, text: str) -> None: ...


class InMemorySupportTopicStore:
    def __init__(self) -> None:
        self._by_telegram_id: dict[int, SupportTopic] = {}
        self._by_thread_id: dict[int, SupportTopic] = {}
        self._context_by_telegram_id: dict[int, SupportContext] = {}

    async def get_context(self, telegram_id: int) -> SupportContext:
        return self._context_by_telegram_id.get(telegram_id, GENERIC_CONTEXT)

    async def save_context(self, telegram_id: int, context: SupportContext) -> None:
        self._context_by_telegram_id[telegram_id] = context

    async def get_by_telegram_id(self, telegram_id: int) -> SupportTopic | None:
        return self._by_telegram_id.get(telegram_id)

    async def get_by_thread_id(self, thread_id: int) -> SupportTopic | None:
        return self._by_thread_id.get(thread_id)

    async def save(self, topic: SupportTopic) -> None:
        self._by_telegram_id[topic.telegram_id] = topic
        self._by_thread_id[topic.thread_id] = topic


def default_topic_title(account: TelegramAccount) -> str:
    return account.full_name.strip() or f"User {account.id}"


class SupportTopicService:
    def __init__(self, *, store: SupportTopicStore, telegram: SupportTopicTelegram, group_id: int) -> None:
        self.store = store
        self.telegram = telegram
        self.group_id = group_id

    async def get_or_create_topic(self, account: TelegramAccount) -> SupportTopic:
        existing = await self.store.get_by_telegram_id(account.id)
        if existing is not None:
            return existing
        context = await self.store.get_context(account.id)
        title = render_topic_title(account, context)
        thread_id = await self.telegram.create_topic(group_id=self.group_id, title=title)
        topic = SupportTopic(telegram_id=account.id, thread_id=thread_id, title=title, context=context)
        await self.store.save(topic)
        await self._pin_metadata(account, topic)
        return topic

    async def record_start_payload(self, account: TelegramAccount, payload: str | None) -> SupportContext:
        context = parse_start_payload(payload)
        await self.store.save_context(account.id, context)
        existing = await self.store.get_by_telegram_id(account.id)
        if existing is not None:
            existing.context = context
            existing.title = render_topic_title(account, context)
            await self.store.save(existing)
            await self._edit_topic_title(existing)
            await self._pin_metadata(account, existing)
        return context

    async def forward_user_text(self, account: TelegramAccount, text: str) -> SupportTopic | None:
        try:
            topic = await self.get_or_create_topic(account)
            await self.telegram.send_topic_text(group_id=self.group_id, thread_id=topic.thread_id, text=text)
        except Exception:
            await self._send_user_failure(account)
            return None
        await self._send_user_ack(account)
        return topic

    async def forward_staff_text(self, *, thread_id: int, text: str) -> SupportTopic | None:
        topic = await self.store.get_by_thread_id(thread_id)
        if topic is None:
            return None
        await self.telegram.send_private_text(telegram_id=topic.telegram_id, text=text)
        return topic

    async def _pin_metadata(self, account: TelegramAccount, topic: SupportTopic) -> None:
        pin_topic_metadata = getattr(self.telegram, "pin_topic_metadata", None)
        if pin_topic_metadata is None:
            return
        await pin_topic_metadata(
            group_id=self.group_id,
            thread_id=topic.thread_id,
            text=render_pinned_metadata(account, topic),
        )

    async def _edit_topic_title(self, topic: SupportTopic) -> None:
        edit_topic_title = getattr(self.telegram, "edit_topic_title", None)
        if edit_topic_title is None:
            return
        await edit_topic_title(group_id=self.group_id, thread_id=topic.thread_id, title=topic.title)

    async def _send_user_ack(self, account: TelegramAccount) -> None:
        send_user_ack = getattr(self.telegram, "send_user_ack", None)
        if send_user_ack is None:
            return
        await send_user_ack(telegram_id=account.id, text=USER_DELIVERY_ACK, ttl_seconds=5)

    async def _send_user_failure(self, account: TelegramAccount) -> None:
        send_user_failure = getattr(self.telegram, "send_user_failure", None)
        if send_user_failure is None:
            return
        await send_user_failure(telegram_id=account.id, text=USER_DELIVERY_FAILURE, persistent=True)
