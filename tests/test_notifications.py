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
    EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
    EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
    EVENT_WITHDRAW_CREATED_ADMIN,
    OUTBOX_STATUS_SENT,
    NotificationService,
)
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


def test_render_assignment_reservation_expired_notification_has_buyer_cta() -> None:
    service = NotificationService(pool=None)  # type: ignore[arg-type]

    rendered = service.render(
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
