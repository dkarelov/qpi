import pytest


def test_parse_start_payload_accepts_qpi_context_refs_without_db_validation() -> None:
    from app.bot.support_context import SupportContext, SupportRef, parse_start_payload

    context = parse_start_payload("seller_listing_L21_S11_TX9")

    assert context == SupportContext(
        role="seller",
        topic="listing",
        refs=(
            SupportRef(kind="L", id=21),
            SupportRef(kind="S", id=11),
            SupportRef(kind="TX", id=9),
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "nonsense",
        "admin_listing_L21",
        "seller_unknown_L21",
        "seller_listing_Loops",
    ],
)
def test_invalid_start_payload_falls_back_to_generic_support(payload: str | None) -> None:
    from app.bot.support_context import GENERIC_CONTEXT, parse_start_payload

    assert parse_start_payload(payload) == GENERIC_CONTEXT


def test_topic_title_places_refs_first_and_truncates_deterministically() -> None:
    from app.bot.support_context import SupportContext, SupportRef, render_topic_title
    from app.bot.support_topics import TelegramAccount

    account = TelegramAccount(id=1001, full_name="Ivan Buyer With A Very Long Name", username="ivan")
    context = SupportContext(
        role="buyer",
        topic="purchase",
        refs=(SupportRef(kind="P", id=12345), SupportRef(kind="L", id=99)),
    )

    title = render_topic_title(account, context, max_length=48)

    assert title == "P12345 L99 · Buyer purchase · Ivan Buyer With..."
    assert len(title) == 48


def test_pinned_metadata_includes_identity_context_refs_and_state() -> None:
    from app.bot.support_context import SupportContext, SupportRef, render_pinned_metadata
    from app.bot.support_topics import SupportTopic, TelegramAccount

    account = TelegramAccount(id=1001, full_name="Ivan Buyer", username="ivan")
    topic = SupportTopic(
        telegram_id=1001,
        thread_id=700,
        title="P31 L21 · Buyer purchase · Ivan Buyer",
        context=SupportContext(
            role="buyer",
            topic="purchase",
            refs=(SupportRef(kind="P", id=31), SupportRef(kind="L", id=21)),
        ),
        status="open",
    )

    metadata = render_pinned_metadata(account, topic)

    assert "Telegram ID: 1001" in metadata
    assert "Username: @ivan" in metadata
    assert "Context: buyer/purchase" in metadata
    assert "Refs: P31, L21" in metadata
    assert "State: open" in metadata


@pytest.mark.asyncio
async def test_start_payload_updates_latest_context_without_creating_topic_until_message() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.created_topics: list[str] = []
            self.pinned_metadata: list[tuple[int, str]] = []
            self.topic_messages: list[tuple[int, int, str]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            assert group_id == -1001234567890
            self.created_topics.append(title)
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.topic_messages.append((group_id, thread_id, text))

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff reply is not part of this test")

        async def pin_topic_metadata(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.pinned_metadata.append((thread_id, text))

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan Buyer", username="ivan")

    await service.record_start_payload(account, "seller_listing_L21_S11")
    await service.record_start_payload(account, "buyer_purchase_P31_L21")

    assert telegram.created_topics == []

    topic = await service.forward_user_text(account, "Нужна помощь")

    assert topic.title == "P31 L21 · Buyer purchase · Ivan Buyer"
    assert telegram.created_topics == ["P31 L21 · Buyer purchase · Ivan Buyer"]
    assert telegram.topic_messages == [(-1001234567890, 701, "Нужна помощь")]
    assert "Context: buyer/purchase" in telegram.pinned_metadata[0][1]
    assert "Refs: P31, L21" in telegram.pinned_metadata[0][1]
