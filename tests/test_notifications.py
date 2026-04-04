from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.config.settings import BotApiSettings
from libs.domain.buyer import BuyerService
from libs.domain.ledger import FinanceService
from libs.domain.models import NotificationOutboxItem
from libs.domain.notifications import (
    EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
    EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
    EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
    EVENT_DEPOSIT_CANCELLED_SELLER,
    EVENT_DEPOSIT_EXPIRED_SELLER,
    EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
    EVENT_MANUAL_BALANCE_CREDIT_TARGET,
    EVENT_SELLER_TOKEN_INVALIDATED,
    EVENT_WITHDRAW_CANCELLED_ADMIN,
    EVENT_WITHDRAW_CREATED_ADMIN,
    EVENT_WITHDRAW_REJECTED_REQUESTER,
    EVENT_WITHDRAW_SENT_REQUESTER,
    OUTBOX_STATUS_SENT,
    NotificationService,
)
from services.bot_api.telegram_notifications import render_telegram_notification
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from tests.e2e_harness import FakeBot, FakeTransport
from tests.helpers import create_account, create_listing, create_shop, create_user


def _build_runtime(database_url: str) -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": database_url,
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [9003],
            "DISPLAY_RUB_PER_USDT": "100",
        }
    )
    return TelegramWebhookRuntime(settings=settings)


def _render_notification(
    item: NotificationOutboxItem,
    *,
    display_rub_per_usdt: Decimal | None = None,
):
    return render_telegram_notification(
        item,
        display_rub_per_usdt=display_rub_per_usdt,
    )


def test_render_assignment_reservation_expired_notification_has_buyer_cta() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=1,
            recipient_telegram_id=1,
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
            dedupe_key="assignment:1:expired_2h:buyer",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "3.000000",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "Бронь истекла" in rendered.text
    assert rendered.parse_mode == "HTML"
    assert rendered.cta_flow == "buyer"
    assert rendered.cta_action == "assignments"
    assert rendered.cta_text == "📋 Покупки"


def test_render_assignment_review_confirmed_seller_notification_contains_rating_and_text() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=11,
            recipient_telegram_id=5,
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER,
            dedupe_key="assignment:1:review_confirmed:seller",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "3.000000",
                "rating": 5,
                "review_text": "Отлично",
                "reviewed_at": "2026-03-18T10:30:00+00:00",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "Отзыв подтвержден" in rendered.text
    assert "Оценка:</b> 5 / 5" in rendered.text
    assert "Текст отзыва:</b> Отлично" in rendered.text
    assert rendered.cta_flow == "seller"
    assert rendered.cta_action == "listing_open"
    assert rendered.cta_entity_id == "2"


def test_render_seller_token_invalidated_notification_uses_neutral_unauthorized_reason() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=2,
            recipient_telegram_id=5,
            recipient_scope="seller",
            event_type=EVENT_SELLER_TOKEN_INVALIDATED,
            dedupe_key="shop:2:token_invalidated:scrapper_401_unauthorized:1",
            payload_json={
                "shop_id": 2,
                "shop_title": "Shop Test",
                "paused_listings_count": 1,
                "source": "scrapper_401_unauthorized",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "WB отклонил авторизацию токена" in rendered.text
    assert "WB отозвал токен" not in rendered.text
    assert rendered.cta_flow == "seller"
    assert rendered.cta_action == "shop_open"


@pytest.mark.parametrize(
    ("event_type", "payload_json", "expected_amount"),
    [
        (
            EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
            {
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "2.538393",
            },
            "~254 ₽",
        ),
        (
            EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER,
            {
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "shop_title": "Магазин",
                "item_count": 2,
                "total_reward_usdt": "5.500000",
            },
            "~550 ₽",
        ),
        (
            EVENT_MANUAL_BALANCE_CREDIT_TARGET,
            {
                "recipient_role": "buyer",
                "amount_usdt": "3.000000",
            },
            "~300 ₽",
        ),
    ],
)
def test_render_buyer_notifications_show_approx_rub_amounts(
    event_type: str,
    payload_json: dict[str, object],
    expected_amount: str,
) -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=3,
            recipient_telegram_id=1,
            recipient_scope="buyer",
            event_type=event_type,
            dedupe_key=f"buyer:{event_type}",
            payload_json=payload_json,
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        ),
        display_rub_per_usdt=Decimal("100"),
    )

    assert expected_amount in rendered.text
    assert "USDT" not in rendered.text


def test_render_seller_reward_unlock_notification_keeps_exact_usdt_amount() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=4,
            recipient_telegram_id=2,
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
            dedupe_key="seller:reward_unlocked",
            payload_json={
                "assignment_id": 1,
                "listing_id": 2,
                "shop_id": 3,
                "display_title": "Товар",
                "shop_title": "Магазин",
                "reward_usdt": "2.538393",
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        ),
        display_rub_per_usdt=Decimal("100"),
    )

    assert "2.538393 USDT" in rendered.text
    assert "~254 ₽" not in rendered.text


def test_render_withdraw_created_admin_notification_uses_full_detail_body() -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=5,
            recipient_telegram_id=9003,
            recipient_scope="admin",
            event_type=EVENT_WITHDRAW_CREATED_ADMIN,
            dedupe_key="withdrawal:3:created:admin:9003",
            payload_json={
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "tech_banker",
                "amount_usdt": "2.538393",
                "status": "withdraw_pending_admin",
                "payout_address": "UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
                "requested_at": "2026-04-03T22:17:00+00:00",
                "processed_at": None,
                "sent_at": None,
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "<b>Новая заявка на вывод</b> · <code>W3</code>" in rendered.text
    assert "<b>Роль:</b> Покупатель" in rendered.text
    assert "<b>Telegram:</b> 2120394 (@tech_banker)" in rendered.text
    assert "<b>Сумма:</b> 2.538393 USDT" in rendered.text
    assert "<b>Статус:</b> 🟡 На проверке" in rendered.text
    assert "<b>Кошелек:</b> UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH" in rendered.text
    assert "<b>Создана:</b> 04.04.2026 01:17 MSK" in rendered.text
    assert "<b>Обработана:</b> -" in rendered.text
    assert "<b>Отправлена:</b> -" in rendered.text
    assert "#" not in rendered.text


@pytest.mark.parametrize(
    ("event_type", "payload_json", "expected_refs"),
    [
        (
            EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
            {
                "chain_tx_id": 11,
                "deposit_intent_id": 22,
                "amount_usdt": "1.200100",
                "reason": "late_payment",
            },
            ["<code>TX11</code>", "<code>D22</code>"],
        ),
        (
            EVENT_DEPOSIT_EXPIRED_SELLER,
            {
                "deposit_intent_id": 22,
                "expected_amount_usdt": "1.200100",
            },
            ["<code>D22</code>"],
        ),
        (
            EVENT_DEPOSIT_CANCELLED_SELLER,
            {
                "deposit_intent_id": 22,
                "reason": "cancelled",
            },
            ["<code>D22</code>"],
        ),
        (
            EVENT_WITHDRAW_CANCELLED_ADMIN,
            {
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "requester_telegram_id": 2120394,
                "requester_username": "tech_banker",
                "amount_usdt": "2.538393",
            },
            ["<code>W3</code>"],
        ),
        (
            EVENT_WITHDRAW_REJECTED_REQUESTER,
            {
                "withdrawal_request_id": 3,
                "requester_role": "buyer",
                "note": "bad address",
            },
            ["<code>W3</code>"],
        ),
        (
            EVENT_WITHDRAW_SENT_REQUESTER,
            {
                "withdrawal_request_id": 3,
                "requester_role": "seller",
                "tx_hash": "0xabc",
            },
            ["<code>W3</code>"],
        ),
    ],
)
def test_render_notifications_use_code_formatted_public_refs(
    event_type: str,
    payload_json: dict[str, object],
    expected_refs: list[str],
) -> None:
    rendered = _render_notification(
        NotificationOutboxItem(
            notification_id=6,
            recipient_telegram_id=1,
            recipient_scope="admin",
            event_type=event_type,
            dedupe_key=f"render:{event_type}",
            payload_json=payload_json,
            status="pending",
            attempt_count=0,
            next_attempt_at=datetime.now(tz=UTC),
            last_error=None,
            sent_at=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    assert "#" not in rendered.text
    for expected_ref in expected_refs:
        assert expected_ref in rendered.text


async def _prepare_assignment(
    db_pool,
    *,
    seller_telegram_id: int,
    buyer_telegram_id: int,
    reward_usdt: Decimal,
    wb_product_id: int = 777,
) -> tuple[BuyerService, FinanceService, int, int, int, int, int]:
    buyer_service = BuyerService(db_pool)
    finance_service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username="seller_test",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="shop-test",
                title="Shop Test",
            )
            listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=wb_product_id,
                reward_usdt=reward_usdt,
                slot_count=1,
                available_slots=1,
                status="active",
                display_title="Тестовый товар",
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt,
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )
            buyer_user_id = await create_user(
                conn,
                telegram_id=buyer_telegram_id,
                role="buyer",
                username="buyer_test",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_user_id,
                account_code=f"user:{buyer_user_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("0.000000"),
            )

    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer_user_id,
        listing_id=listing_id,
        idempotency_key=f"reserve:{buyer_user_id}:{listing_id}",
    )
    return (
        buyer_service,
        finance_service,
        reservation.assignment_id,
        seller_collateral_account_id,
        reward_reserved_account_id,
        buyer_available_account_id,
        listing_id,
    )


@pytest.mark.asyncio
async def test_create_withdrawal_request_enqueues_admin_notification(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(conn, telegram_id=9001, role="buyer", username="buyer")
            await create_user(conn, telegram_id=9003, role="admin", username="admin")
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("5.000000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    result = await service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("5.000000"),
        payout_address="UQ_TEST_ADDRESS",
        idempotency_key="withdraw-create-notify",
    )

    assert result.created is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_telegram_id, event_type
                FROM notification_outbox
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {
            "recipient_telegram_id": 9003,
            "event_type": EVENT_WITHDRAW_CREATED_ADMIN,
        }
    ]


@pytest.mark.asyncio
async def test_reservation_expiry_enqueues_buyer_only(db_pool) -> None:
    buyer_service, _, assignment_id, _, _, _, _ = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9101,
        buyer_telegram_id=9102,
        reward_usdt=Decimal("4.000000"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET reservation_expires_at = %s
                    WHERE id = %s
                    """,
                    (datetime.now(tz=UTC) - timedelta(minutes=1), assignment_id),
                )

    result = await buyer_service.process_expired_reservations(batch_size=10)
    assert result.expired_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_scope, event_type
                FROM notification_outbox
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {
            "recipient_scope": "buyer",
            "event_type": EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
        }
    ]


@pytest.mark.asyncio
async def test_reward_unlock_enqueues_buyer_and_seller_notifications(db_pool) -> None:
    (
        _,
        finance_service,
        assignment_id,
        _,
        reward_reserved_account_id,
        buyer_available_account_id,
        _,
    ) = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9201,
        buyer_telegram_id=9202,
        reward_usdt=Decimal("7.500000"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'picked_up_wait_unlock',
                        unlock_at = timezone('utc', now()) - interval '1 minute'
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )

    result = await finance_service.unlock_assignment_reward(
        assignment_id=assignment_id,
        buyer_available_account_id=buyer_available_account_id,
        reward_reserved_account_id=reward_reserved_account_id,
        idempotency_key="unlock-notify",
    )
    assert result.changed is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT recipient_scope, event_type
                FROM notification_outbox
                ORDER BY recipient_scope ASC
                """
            )
            rows = await cur.fetchall()

    assert rows == [
        {"recipient_scope": "buyer", "event_type": EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER},
        {"recipient_scope": "seller", "event_type": EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER},
    ]


@pytest.mark.asyncio
async def test_runtime_dispatch_sends_and_marks_notification_sent(
    db_pool, isolated_database: str
) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    transport = FakeTransport()
    bot = FakeBot(transport=transport)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                buyer_user_id = await create_user(
                    conn,
                    telegram_id=9301,
                    role="buyer",
                    username="buyer",
                )
                seller_user_id = await create_user(
                    conn,
                    telegram_id=9302,
                    role="seller",
                    username="seller",
                )
                shop_id = await create_shop(
                    conn,
                    seller_user_id=seller_user_id,
                    slug="dispatch-shop",
                    title="Dispatch",
                )
                listing_id = await create_listing(
                    conn,
                    shop_id=shop_id,
                    seller_user_id=seller_user_id,
                    wb_product_id=9303,
                    reward_usdt=Decimal("3.000000"),
                    slot_count=1,
                    available_slots=1,
                    status="active",
                    display_title="Товар для уведомления",
                )
                await cur.execute(
                    """
                    INSERT INTO assignments (
                        listing_id,
                        buyer_user_id,
                        wb_product_id,
                        status,
                        reward_usdt,
                        reservation_expires_at,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, 'reserved', %s, timezone('utc', now()), %s)
                    RETURNING id
                    """,
                    (
                        listing_id,
                        buyer_user_id,
                        9303,
                        Decimal("3.000000"),
                        "dispatch-assignment",
                    ),
                )
                assignment_id = int((await cur.fetchone())["id"])
                await (
                    runtime._notification_service.enqueue_assignment_reservation_expired_for_buyer_locked(
                        cur,
                        assignment_id=assignment_id,
                    )
                )

    await runtime._dispatch_notifications_once(bot=bot)

    bot_events = transport.find("bot_send")
    assert len(bot_events) == 1
    assert "Бронь истекла" in (bot_events[0].text or "")
    assert bot_events[0].parse_mode == "HTML"
    assert bot_events[0].reply_markup is not None

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM notification_outbox
                """
            )
            row = await cur.fetchone()

    assert row["status"] == OUTBOX_STATUS_SENT


@pytest.mark.asyncio
async def test_runtime_dispatch_formats_buyer_reward_notification_in_rub(
    db_pool, isolated_database: str
) -> None:
    runtime = _build_runtime(isolated_database)
    runtime._notification_service = NotificationService(db_pool)
    transport = FakeTransport()
    bot = FakeBot(transport=transport)

    (
        _,
        finance_service,
        assignment_id,
        _,
        reward_reserved_account_id,
        buyer_available_account_id,
        _,
    ) = await _prepare_assignment(
        db_pool,
        seller_telegram_id=9401,
        buyer_telegram_id=9402,
        reward_usdt=Decimal("2.538393"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'picked_up_wait_unlock',
                        unlock_at = timezone('utc', now()) - interval '1 minute'
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )

    result = await finance_service.unlock_assignment_reward(
        assignment_id=assignment_id,
        buyer_available_account_id=buyer_available_account_id,
        reward_reserved_account_id=reward_reserved_account_id,
        idempotency_key="unlock-notify-rub",
    )
    assert result.changed is True

    await runtime._dispatch_notifications_once(bot=bot)

    buyer_events = [event for event in transport.find("bot_send") if event.chat_id == 9402]
    assert len(buyer_events) == 1
    assert "Кэшбэк зачислен" in (buyer_events[0].text or "")
    assert "~254 ₽" in (buyer_events[0].text or "")
    assert "USDT" not in (buyer_events[0].text or "")
