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


def test_topic_title_places_name_first_and_truncates_deterministically() -> None:
    from app.bot.support_context import SupportContext, SupportRef, render_topic_title
    from app.bot.support_topics import TelegramAccount

    account = TelegramAccount(id=1001, full_name="Karina Seller With A Very Long Name", username="karina")
    context = SupportContext(
        role="seller",
        topic="listing",
        refs=(SupportRef(kind="L", id=21), SupportRef(kind="S", id=11)),
    )

    title = render_topic_title(account, context, max_length=48)

    assert title == "Karina Seller With A Very Long Name · Seller..."
    assert len(title) <= 48


@pytest.mark.asyncio
async def test_start_payload_updates_title_without_pinning_metadata() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.created_topics: list[str] = []
            self.pinned_metadata_calls: list[tuple[int, str]] = []
            self.topic_messages: list[tuple[int, int, str]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            assert group_id == -1001234567890
            self.created_topics.append(title)
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.topic_messages.append((group_id, thread_id, text))

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff reply is not part of this test")

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

        async def pin_topic_metadata(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.pinned_metadata_calls.append((thread_id, text))

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

    assert topic.title == "Ivan Buyer · Buyer purchase · P31 L21"
    assert telegram.created_topics == ["Ivan Buyer · Buyer purchase · P31 L21"]
    assert telegram.topic_messages == [(-1001234567890, 701, "Нужна помощь")]
    assert telegram.pinned_metadata_calls == []
