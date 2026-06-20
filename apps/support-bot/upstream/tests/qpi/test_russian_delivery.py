import pytest


def test_private_user_texts_are_russian_even_for_english_telegram_locale() -> None:
    from app.bot.utils.texts import TextMessage

    text = TextMessage("en")

    assert "Здравствуйте" in text.get("main_menu")
    assert "Сообщение отправлено" in text.get("message_sent")
    assert "Message sent" not in text.get("message_sent")


@pytest.mark.asyncio
async def test_user_success_ack_is_sent_only_after_topic_delivery() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            self.events.append(f"create:{title}")
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.events.append(f"topic:{text}")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            self.events.append(f"private:{text}")

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            assert ttl_seconds == 5
            self.events.append(f"ack:{text}")

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )

    await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Нужна помощь")

    assert telegram.events == [
        "create:Ivan",
        "topic:Нужна помощь",
        "ack:Сообщение отправлено в поддержку. Ответим здесь.",
    ]


@pytest.mark.asyncio
async def test_user_delivery_failure_is_persistent_russian_and_hides_exception_details() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.failures: list[tuple[str, bool]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            raise RuntimeError("database password leaked in internal stack")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff reply is not part of this test")

        async def send_user_failure(self, *, telegram_id: int, text: str, persistent: bool) -> None:
            self.failures.append((text, persistent))

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )

    result = await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Нужна помощь")

    assert result is None
    assert telegram.failures == [
        ("Не удалось отправить сообщение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут.", True)
    ]
    assert "password" not in telegram.failures[0][0]
    assert "stack" not in telegram.failures[0][0]
