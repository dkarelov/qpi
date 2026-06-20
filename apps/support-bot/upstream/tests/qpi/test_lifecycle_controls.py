import pytest


@pytest.mark.asyncio
async def test_closed_topic_reopens_same_topic_on_next_user_message() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.created = 0
            self.reopened: list[int] = []
            self.topic_messages: list[tuple[int, str]] = []
            self.private_messages: list[str] = []
            self.metadata_calls: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            self.created += 1
            return 701

        async def close_topic(self, *, group_id: int, thread_id: int) -> None:
            return None

        async def reopen_topic(self, *, group_id: int, thread_id: int) -> None:
            self.reopened.append(thread_id)

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.topic_messages.append((thread_id, text))

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            self.private_messages.append(text)

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def pin_topic_metadata(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.metadata_calls.append(text)

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan", username="ivan")
    topic = await service.forward_user_text(account, "Первый вопрос")

    await service.close_topic(thread_id=topic.thread_id)
    reopened = await service.forward_user_text(account, "Новый вопрос")

    assert reopened == topic
    assert telegram.created == 1
    assert telegram.reopened == [topic.thread_id]
    assert telegram.private_messages == []
    assert telegram.topic_messages == [(topic.thread_id, "Первый вопрос"), (topic.thread_id, "Новый вопрос")]
    assert topic.status == "open"
    assert telegram.metadata_calls == []


@pytest.mark.asyncio
async def test_failed_reopen_keeps_topic_closed_in_storage() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.failures: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def close_topic(self, *, group_id: int, thread_id: int) -> None:
            return None

        async def reopen_topic(self, *, group_id: int, thread_id: int) -> None:
            raise RuntimeError("telegram is unavailable")

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            return None

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            return None

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def send_user_failure(self, *, telegram_id: int, text: str, persistent: bool) -> None:
            self.failures.append(text)

    store = InMemorySupportTopicStore()
    telegram = FakeTelegram()
    service = SupportTopicService(
        store=store,
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan")
    topic = await service.forward_user_text(account, "Первый вопрос")
    await service.close_topic(thread_id=topic.thread_id)

    result = await service.forward_user_text(account, "Новый вопрос")
    stored = await store.get_by_thread_id(topic.thread_id)

    assert result is None
    assert telegram.failures == ["Не удалось отправить сообщение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут."]
    assert stored is not None
    assert stored.status == "closed"


@pytest.mark.asyncio
async def test_banned_user_messages_are_silently_ignored() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.topic_messages: list[str] = []
            self.failures: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.topic_messages.append(text)

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            return None

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def send_user_failure(self, *, telegram_id: int, text: str, persistent: bool) -> None:
            self.failures.append(text)

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan")
    topic = await service.forward_user_text(account, "Первый вопрос")

    await service.set_banned(thread_id=topic.thread_id, is_banned=True)
    result = await service.forward_user_text(account, "Новый вопрос")

    assert result is None
    assert telegram.topic_messages == ["Первый вопрос"]
    assert telegram.failures == []


@pytest.mark.asyncio
async def test_silent_topic_suppresses_staff_reply_delivery() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.private_messages: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            return None

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            self.private_messages.append(text)

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    topic = await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Первый вопрос")

    await service.set_silent(thread_id=topic.thread_id, is_silent=True)
    result = await service.forward_staff_text(thread_id=topic.thread_id, text="Ответ поддержки")

    assert result == topic
    assert telegram.private_messages == []


@pytest.mark.asyncio
async def test_escalation_sets_state_and_notifies_developer_without_pinning_metadata() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.dev_notifications: list[str] = []
            self.metadata_calls: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            return None

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            return None

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def notify_developer(self, *, text: str) -> None:
            self.dev_notifications.append(text)

        async def pin_topic_metadata(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.metadata_calls.append(text)

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    topic = await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Первый вопрос")

    escalated = await service.escalate_topic(thread_id=topic.thread_id)

    assert escalated is topic
    assert topic.status == "escalated"
    assert telegram.dev_notifications == ["Escalated Support Topic for Telegram ID 1001"]
    assert telegram.metadata_calls == []
