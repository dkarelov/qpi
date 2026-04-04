from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.buyer import BuyerService
from libs.domain.order_tracker import OrderTrackerService
from tests.helpers import create_account, create_listing, create_shop, create_user


def _encode_payload(
    *,
    order_id: str,
    ordered_at: datetime,
) -> str:
    payload = [order_id, ordered_at.replace(tzinfo=None).isoformat()]
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _build_tracker_service(
    db_pool,
    *,
    lock_conninfo: str,
    lock_id: int,
) -> OrderTrackerService:
    return OrderTrackerService(
        db_pool,
        advisory_lock_conninfo=lock_conninfo,
        advisory_lock_id=lock_id,
        reservation_expiry_batch_size=100,
        wb_event_batch_size=100,
        delivery_expiry_batch_size=100,
        unlock_batch_size=100,
        delivery_expiry_days=60,
        unlock_days=15,
    )


async def _insert_wb_report_row(
    db_pool,
    *,
    rrd_id: int,
    srid: str,
    supplier_oper_name: str,
    event_at: datetime,
    nm_id: int,
    order_uid: str | None = None,
) -> None:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO wb_report_rows (
                        rrd_id,
                        wb_srid,
                        order_uid,
                        nm_id,
                        supplier_oper_name,
                        sale_dt,
                        order_dt,
                        create_dt
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        rrd_id,
                        srid,
                        order_uid,
                        nm_id,
                        supplier_oper_name,
                        event_at,
                        event_at,
                        event_at,
                    ),
                )


async def _prepare_order_verified_assignment(
    db_pool,
    *,
    seller_telegram_id: int,
    buyer_telegram_id: int,
    order_id: str,
    wb_product_id: int,
    ordered_at: datetime,
    reward_usdt: Decimal,
    review_required: bool = False,
    review_phrases: list[str] | None = None,
) -> dict[str, int]:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username=f"seller_{seller_telegram_id}",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug=f"shop-{seller_telegram_id}",
                title=f"Shop {seller_telegram_id}",
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
                review_phrases=review_phrases,
            )
            await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt,
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )

    buyer = await buyer_service.bootstrap_buyer(
        telegram_id=buyer_telegram_id,
        username=f"buyer_{buyer_telegram_id}",
    )
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=listing_id,
        idempotency_key=f"reserve:{buyer_telegram_id}:{wb_product_id}",
    )
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET review_required = %s
                    WHERE id = %s
                    """,
                    (review_required, reservation.assignment_id),
                )
    payload_base64 = _encode_payload(
        order_id=order_id,
        ordered_at=ordered_at,
    )
    await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        payload_base64=payload_base64,
    )
    return {
        "seller_user_id": seller_user_id,
        "buyer_user_id": buyer.user_id,
        "listing_id": listing_id,
        "assignment_id": reservation.assignment_id,
        "wb_product_id": wb_product_id,
    }


async def _prepare_reserved_assignment(
    db_pool,
    *,
    seller_telegram_id: int,
    buyer_telegram_id: int,
    wb_product_id: int,
    reward_usdt: Decimal,
) -> dict[str, int]:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username=f"seller_{seller_telegram_id}",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug=f"shop-{seller_telegram_id}",
                title=f"Shop {seller_telegram_id}",
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
            )
            await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt,
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )

    buyer = await buyer_service.bootstrap_buyer(
        telegram_id=buyer_telegram_id,
        username=f"buyer_{buyer_telegram_id}",
    )
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=listing_id,
        idempotency_key=f"reserve:{buyer_telegram_id}:{wb_product_id}",
    )
    return {
        "seller_user_id": seller_user_id,
        "buyer_user_id": buyer.user_id,
        "listing_id": listing_id,
        "assignment_id": reservation.assignment_id,
        "wb_product_id": wb_product_id,
    }


@pytest.mark.asyncio
async def test_order_tracker_sale_matches_order_uid_when_wb_srid_has_prefix(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    order_uid = "85ad7978a30147eca5278c9cc0f5f967"
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910011,
        buyer_telegram_id=920011,
        order_id=order_uid,
        wb_product_id=670011,
        ordered_at=now - timedelta(days=2),
        reward_usdt=Decimal("10.000000"),
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810011,
        srid=f"ebs.{order_uid}.0.0",
        order_uid=order_uid,
        supplier_oper_name="Продажа",
        event_at=now - timedelta(days=1),
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500611)
    result = await service.run_once()

    assert result.wb_pickup_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, pickup_at, unlock_at
                FROM assignments
                WHERE id = %s
                """,
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "picked_up_wait_unlock"
            assert assignment["pickup_at"] is not None
            assert assignment["unlock_at"] is not None


@pytest.mark.asyncio
async def test_order_tracker_sale_moves_review_required_assignment_to_pickup_wait_review(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910021,
        buyer_telegram_id=920021,
        order_id="order-sale-review",
        wb_product_id=670021,
        ordered_at=now - timedelta(days=2),
        reward_usdt=Decimal("9.000000"),
        review_required=True,
        review_phrases=["в размер", "не садятся после стирки", "хорошая ткань"],
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810021,
        srid="order-sale-review",
        supplier_oper_name="Продажа",
        event_at=now - timedelta(days=1),
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500621)
    result = await service.run_once()

    assert result.wb_pickup_count == 1
    assert result.unlock_changed_count == 0

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, pickup_at, unlock_at, review_phrases_json
                FROM assignments
                WHERE id = %s
                """,
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "picked_up_wait_review"
            assert assignment["pickup_at"] is not None
            assert assignment["unlock_at"] is not None
            assert len(assignment["review_phrases_json"]) == 2
            assert set(assignment["review_phrases_json"]).issubset(
                {"в размер", "не садятся после стирки", "хорошая ткань"}
            )


@pytest.mark.asyncio
async def test_order_tracker_sale_moves_to_pickup_and_unlocks_after_15_days(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910001,
        buyer_telegram_id=920001,
        order_id="order-sale-1",
        wb_product_id=670001,
        ordered_at=now - timedelta(days=20),
        reward_usdt=Decimal("10.000000"),
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810001,
        srid="order-sale-1",
        supplier_oper_name="Продажа",
        event_at=now - timedelta(days=16),
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500601)
    result = await service.run_once()

    assert result.lock_acquired is True
    assert result.wb_pickup_count == 1
    assert result.unlock_changed_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, pickup_at, unlock_at
                FROM assignments
                WHERE id = %s
                """,
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "withdraw_sent"
            assert assignment["pickup_at"] is not None
            assert assignment["unlock_at"] is not None
            assert assignment["unlock_at"] - assignment["pickup_at"] == timedelta(days=15)

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{fixture['buyer_user_id']}:buyer_available",),
            )
            buyer_available = await cur.fetchone()
            assert buyer_available["current_balance_usdt"] == Decimal("10.000000")

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = 'system:reward_reserved'
                """
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("0.000000")


@pytest.mark.asyncio
async def test_order_tracker_return_cancels_order_verified_assignment(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910002,
        buyer_telegram_id=920002,
        order_id="order-return-1",
        wb_product_id=670002,
        ordered_at=now - timedelta(days=10),
        reward_usdt=Decimal("9.000000"),
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810002,
        srid="order-return-1",
        supplier_oper_name="Возврат",
        event_at=now - timedelta(days=1),
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500602)
    result = await service.run_once()

    assert result.wb_return_cancelled_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "returned_within_14d"

            await cur.execute(
                "SELECT available_slots FROM listings WHERE id = %s",
                (fixture["listing_id"],),
            )
            listing = await cur.fetchone()
            assert listing["available_slots"] == 1

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{fixture['seller_user_id']}:seller_collateral",),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("9.000000")


@pytest.mark.asyncio
async def test_order_tracker_ignores_return_after_unlock_window_and_unlocks(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910003,
        buyer_telegram_id=920003,
        order_id="order-return-late-1",
        wb_product_id=670003,
        ordered_at=now - timedelta(days=20),
        reward_usdt=Decimal("8.000000"),
    )
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'picked_up_wait_unlock',
                        pickup_at = %s,
                        unlock_at = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (now - timedelta(days=20), now - timedelta(days=1), fixture["assignment_id"]),
                )

    await _insert_wb_report_row(
        db_pool,
        rrd_id=810003,
        srid="order-return-late-1",
        supplier_oper_name="Возврат",
        event_at=now,
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500603)
    result = await service.run_once()

    assert result.wb_return_ignored_after_unlock_count == 1
    assert result.unlock_changed_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "withdraw_sent"


@pytest.mark.asyncio
async def test_order_tracker_ignores_sale_when_nm_id_mismatches_listing_product(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910031,
        buyer_telegram_id=920031,
        order_id="order-sale-mismatch-1",
        wb_product_id=670031,
        ordered_at=now - timedelta(days=20),
        reward_usdt=Decimal("8.000000"),
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810031,
        srid="order-sale-mismatch-1",
        supplier_oper_name="Продажа",
        event_at=now - timedelta(days=16),
        nm_id=670099,
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500631)
    result = await service.run_once()

    assert result.wb_pickup_count == 0
    assert result.unlock_changed_count == 0

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "order_verified"


@pytest.mark.asyncio
async def test_order_tracker_marks_delivery_expired_after_60_days_without_pickup(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910004,
        buyer_telegram_id=920004,
        order_id="order-delivery-expired-1",
        wb_product_id=670004,
        ordered_at=now - timedelta(days=61),
        reward_usdt=Decimal("7.000000"),
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500604)
    result = await service.run_once()

    assert result.delivery_expired_changed_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "delivery_expired"

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{fixture['seller_user_id']}:seller_collateral",),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("7.000000")


@pytest.mark.asyncio
async def test_order_tracker_ignores_correction_operations_in_mvp(
    db_pool,
    isolated_database: str,
) -> None:
    now = datetime.now(UTC)
    fixture = await _prepare_order_verified_assignment(
        db_pool,
        seller_telegram_id=910005,
        buyer_telegram_id=920005,
        order_id="order-correction-1",
        wb_product_id=670005,
        ordered_at=now - timedelta(days=2),
        reward_usdt=Decimal("6.000000"),
    )
    await _insert_wb_report_row(
        db_pool,
        rrd_id=810005,
        srid="order-correction-1",
        supplier_oper_name="Коррекция продаж",
        event_at=now - timedelta(days=1),
        nm_id=fixture["wb_product_id"],
    )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500605)
    result = await service.run_once()

    assert result.wb_processed_count == 0
    assert result.wb_pickup_count == 0
    assert result.wb_return_cancelled_count == 0

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "order_verified"


@pytest.mark.asyncio
async def test_order_tracker_processes_reservation_expiry_instead_of_worker(
    db_pool,
    isolated_database: str,
) -> None:
    fixture = await _prepare_reserved_assignment(
        db_pool,
        seller_telegram_id=910006,
        buyer_telegram_id=920006,
        wb_product_id=670006,
        reward_usdt=Decimal("5.000000"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET reservation_expires_at = timezone('utc', now()) - interval '1 minute',
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (fixture["assignment_id"],),
                )

    service = _build_tracker_service(db_pool, lock_conninfo=isolated_database, lock_id=500606)
    result = await service.run_once()

    assert result.reservation_expiry_changed_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (fixture["assignment_id"],),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "expired_2h"
