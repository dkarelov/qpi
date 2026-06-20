from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
    status: str = "open"
    is_banned: bool = False


class SupportTopicStore(Protocol):
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
        title = default_topic_title(account)
        thread_id = await self.telegram.create_topic(group_id=self.group_id, title=title)
        topic = SupportTopic(telegram_id=account.id, thread_id=thread_id, title=title)
        await self.store.save(topic)
        return topic

    async def forward_user_text(self, account: TelegramAccount, text: str) -> SupportTopic:
        topic = await self.get_or_create_topic(account)
        await self.telegram.send_topic_text(group_id=self.group_id, thread_id=topic.thread_id, text=text)
        return topic

    async def forward_staff_text(self, *, thread_id: int, text: str) -> SupportTopic | None:
        topic = await self.store.get_by_thread_id(thread_id)
        if topic is None:
            return None
        await self.telegram.send_private_text(telegram_id=topic.telegram_id, text=text)
        return topic
