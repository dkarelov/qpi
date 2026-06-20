import pytest


@pytest.mark.asyncio
async def test_user_media_forwarding_preserves_kind_file_id_caption_and_ack_order() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, MediaItem, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            self.events.append(("create", title))
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            raise AssertionError("text path is not part of this test")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff text path is not part of this test")

        async def send_topic_media(self, *, group_id: int, thread_id: int, media: MediaItem) -> None:
            self.events.append(("topic_media", group_id, thread_id, media))

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            self.events.append(("ack", text, ttl_seconds))

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    media = MediaItem(kind="photo", file_id="photo-file", caption="Чек покупки")

    topic = await service.forward_user_media(TelegramAccount(id=1001, full_name="Ivan"), media)

    assert topic is not None
    assert telegram.events == [
        ("create", "Ivan"),
        ("topic_media", -1001234567890, 701, media),
        ("ack", "Сообщение отправлено в поддержку. Ответим здесь.", 5),
    ]


@pytest.mark.asyncio
async def test_user_album_forwarding_preserves_grouping_and_captions() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, MediaItem, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.albums: list[tuple[int, int, tuple[MediaItem, ...]]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            raise AssertionError("text path is not part of this test")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff text path is not part of this test")

        async def send_topic_album(self, *, group_id: int, thread_id: int, media: tuple[MediaItem, ...]) -> None:
            self.albums.append((group_id, thread_id, media))

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    album = (
        MediaItem(kind="photo", file_id="photo-1", caption="Первое фото", media_group_id="g1"),
        MediaItem(kind="photo", file_id="photo-2", caption="Второе фото", media_group_id="g1"),
    )

    await service.forward_user_album(TelegramAccount(id=1001, full_name="Ivan"), album)

    assert telegram.albums == [(-1001234567890, 701, album)]


@pytest.mark.asyncio
async def test_staff_media_reply_is_forwarded_back_to_private_user() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, MediaItem, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.private_media: list[tuple[int, MediaItem]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            return None

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff text path is not part of this test")

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def send_private_media(self, *, telegram_id: int, media: MediaItem) -> None:
            self.private_media.append((telegram_id, media))

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan")
    topic = await service.forward_user_text(account, "Нужна помощь")
    media = MediaItem(kind="document", file_id="doc-file", caption="Инструкция")

    result = await service.forward_staff_media(thread_id=topic.thread_id, media=media)

    assert result == topic
    assert telegram.private_media == [(1001, media)]
