from __future__ import annotations

from typing import Protocol

from app.bot.support_topics import TelegramAccount


class NewsletterRegistry(Protocol):
    def add(self, telegram_id: int) -> None: ...

    def all_ids(self) -> list[int]: ...


class InMemoryNewsletterRegistry:
    def __init__(self) -> None:
        self._ids: set[int] = set()

    def add(self, telegram_id: int) -> None:
        self._ids.add(telegram_id)

    def all_ids(self) -> list[int]:
        return sorted(self._ids)


class NewsletterService:
    """Optional newsletter registration surface, disabled until explicitly used."""

    def __init__(self, registry: NewsletterRegistry) -> None:
        self.registry = registry

    def register(self, account: TelegramAccount) -> None:
        self.registry.add(account.id)

    def subscriber_ids(self) -> list[int]:
        return self.registry.all_ids()
