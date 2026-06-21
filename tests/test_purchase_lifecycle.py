from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg.rows import dict_row

from libs.domain.errors import DuplicateOrderError, InvalidStateError
from libs.domain.purchase_lifecycle import PurchaseLifecycleService
from tests.helpers import create_account, create_listing, create_shop, create_user


def _encode_order_payload(*, task_uuid: str, order_id: str, ordered_at: str = "2026-02-26T12:00:00Z") -> str:
    payload: list[Any] = [task_uuid, order_id, ordered_at]
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _encode_review_payload(
    *,
    task_uuid: str,
    rating: int = 5,
    review_text: str = "хорошая ткань, в размер",
    reviewed_at: str = "2026-03-18T10:30:00Z",
) -> str:
    payload: list[Any] = [task_uuid, reviewed_at, rating, review_text]
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


async def _setup_active_announcement(
    db_pool,
    *,
    seller_telegram_id: int = 620001,
    buyer_telegram_id: int = 620002,
    wb_product_id: int = 620003,
    reward_usdt: Decimal = Decimal("5.000000"),
    slot_count: int = 2,
    review_phrases: list[str] | None = None,
) -> dict[str, int]:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username=f"seller_{seller_telegram_id}",
            )
            buyer_user_id = await create_user(
                conn,
                telegram_id=buyer_telegram_id,
                role="buyer",
                username=f"buyer_{buyer_telegram_id}",
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
                slot_count=slot_count,
                available_slots=slot_count,
                status="active",
                review_phrases=review_phrases,
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt * slot_count,
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )
            await conn.execute(
                """
                INSERT INTO balance_holds (
                    account_id,
                    hold_type,
                    status,
                    amount_usdt,
                    listing_id,
                    idempotency_key
                )
                VALUES (%s, 'collateral', 'active', %s, %s, %s)
                """,
                (
                    seller_collateral_account_id,
                    reward_usdt * slot_count,
                    listing_id,
                    f"test-collateral:{listing_id}",
                ),
            )

    return {
        "seller_user_id": seller_user_id,
        "buyer_user_id": buyer_user_id,
        "shop_id": shop_id,
        "listing_id": listing_id,
        "seller_collateral_account_id": seller_collateral_account_id,
        "reward_reserved_account_id": reward_reserved_account_id,
    }


@pytest.mark.asyncio
async def test_purchase_lifecycle_reserve_is_idempotent_and_moves_cashback(db_pool) -> None:
    fixture = await _setup_active_announcement(db_pool)
    lifecycle = PurchaseLifecycleService(db_pool)

    first = await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-reserve-idempotent",
    )
    second = await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-reserve-idempotent",
    )

    assert first.created is True
    assert second.created is False
    assert second.purchase_id == first.purchase_id
    assert second.cashback_usdt == Decimal("5.000000")

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT available_slots FROM listings WHERE id = %s", (fixture["listing_id"],))
            listing = await cur.fetchone()
            assert listing["available_slots"] == 1

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["seller_collateral_account_id"],),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("5.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["reward_reserved_account_id"],),
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("5.000000")

            await cur.execute(
                """
                SELECT hold_type, status, amount_usdt
                FROM balance_holds
                WHERE assignment_id = %s
                """,
                (first.purchase_id,),
            )
            hold = await cur.fetchone()
            assert hold == {
                "hold_type": "slot_reserve",
                "status": "active",
                "amount_usdt": Decimal("5.000000"),
            }


@pytest.mark.asyncio
async def test_purchase_lifecycle_reserve_rejects_already_purchased_before_no_slots(db_pool) -> None:
    fixture = await _setup_active_announcement(
        db_pool,
        seller_telegram_id=620011,
        buyer_telegram_id=620012,
        wb_product_id=620013,
        slot_count=1,
    )
    lifecycle = PurchaseLifecycleService(db_pool)

    reservation = await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-reserve-precedence-purchased-first",
    )
    await lifecycle.submit_order_proof(
        buyer_user_id=fixture["buyer_user_id"],
        purchase_id=reservation.purchase_id,
        token_payload=_encode_order_payload(task_uuid=str(reservation.task_uuid), order_id="ORD-PRECEDENCE"),
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            duplicate_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=620013,
                reward_usdt=Decimal("5.000000"),
                slot_count=1,
                available_slots=0,
                status="active",
            )

    with pytest.raises(InvalidStateError, match="already purchased"):
        await lifecycle.reserve_purchase(
            buyer_user_id=fixture["buyer_user_id"],
            announcement_id=duplicate_listing_id,
            idempotency_seed="purchase-reserve-precedence-purchased-second",
        )


@pytest.mark.asyncio
async def test_purchase_lifecycle_reserve_rejects_active_assignment_before_no_slots(db_pool) -> None:
    fixture = await _setup_active_announcement(
        db_pool,
        seller_telegram_id=620021,
        buyer_telegram_id=620022,
        wb_product_id=620023,
        slot_count=1,
    )
    lifecycle = PurchaseLifecycleService(db_pool)

    await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-reserve-precedence-active-first",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            duplicate_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=620023,
                reward_usdt=Decimal("5.000000"),
                slot_count=1,
                available_slots=0,
                status="active",
            )

    with pytest.raises(InvalidStateError, match="already has assignment"):
        await lifecycle.reserve_purchase(
            buyer_user_id=fixture["buyer_user_id"],
            announcement_id=duplicate_listing_id,
            idempotency_seed="purchase-reserve-precedence-active-second",
        )


@pytest.mark.asyncio
async def test_purchase_lifecycle_direct_order_proof_rejects_duplicate_order(db_pool) -> None:
    first_fixture = await _setup_active_announcement(
        db_pool,
        seller_telegram_id=621001,
        buyer_telegram_id=621002,
        wb_product_id=621003,
        slot_count=2,
    )
    async with db_pool.connection() as conn:
        async with conn.transaction():
            second_buyer_id = await create_user(
                conn,
                telegram_id=621004,
                role="buyer",
                username="buyer_duplicate_order",
            )

    lifecycle = PurchaseLifecycleService(db_pool)
    first = await lifecycle.reserve_purchase(
        buyer_user_id=first_fixture["buyer_user_id"],
        announcement_id=first_fixture["listing_id"],
        idempotency_seed="purchase-order-first",
    )
    second = await lifecycle.reserve_purchase(
        buyer_user_id=second_buyer_id,
        announcement_id=first_fixture["listing_id"],
        idempotency_seed="purchase-order-second",
    )

    first_result = await lifecycle.submit_order_proof_by_task_uuid(
        buyer_user_id=first_fixture["buyer_user_id"],
        token_payload=_encode_order_payload(task_uuid=str(first.task_uuid), order_id="ORD-DUPLICATE"),
    )
    assert first_result.changed is True
    assert first_result.status == "order_verified"

    with pytest.raises(DuplicateOrderError):
        await lifecycle.submit_order_proof_by_task_uuid(
            buyer_user_id=second_buyer_id,
            token_payload=_encode_order_payload(task_uuid=str(second.task_uuid), order_id="ORD-DUPLICATE"),
        )


@pytest.mark.asyncio
async def test_purchase_lifecycle_review_correction_and_unlocks_cashback(db_pool) -> None:
    fixture = await _setup_active_announcement(
        db_pool,
        seller_telegram_id=622001,
        buyer_telegram_id=622002,
        wb_product_id=622003,
        review_phrases=["хорошая ткань", "в размер"],
    )
    lifecycle = PurchaseLifecycleService(db_pool)

    reservation = await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-review-reserve",
    )
    await lifecycle.submit_order_proof(
        buyer_user_id=fixture["buyer_user_id"],
        purchase_id=reservation.purchase_id,
        token_payload=_encode_order_payload(task_uuid=str(reservation.task_uuid), order_id="ORD-REVIEW"),
    )
    picked_up = await lifecycle.mark_picked_up(
        purchase_id=reservation.purchase_id,
        pickup_at=datetime.now(tz=UTC) - timedelta(days=16),
        unlock_days=15,
    )
    assert picked_up is True

    pending = await lifecycle.submit_review_confirmation(
        buyer_user_id=fixture["buyer_user_id"],
        purchase_id=reservation.purchase_id,
        token_payload=_encode_review_payload(
            task_uuid=str(reservation.task_uuid),
            rating=4,
            review_text="текст без обязательных фраз",
        ),
    )
    assert pending.status == "picked_up_wait_review"
    assert pending.verification_status == "pending_manual"

    corrected = await lifecycle.submit_review_confirmation_by_task_uuid(
        buyer_user_id=fixture["buyer_user_id"],
        token_payload=_encode_review_payload(task_uuid=str(reservation.task_uuid)),
    )
    assert corrected.status == "picked_up_wait_unlock"
    assert corrected.verification_status == "verified_auto"

    unlocked = await lifecycle.unlock_cashback(
        purchase_id=reservation.purchase_id,
        idempotency_seed="purchase-review-unlock",
    )
    assert unlocked.changed is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM assignments
                WHERE id = %s
                """,
                (reservation.purchase_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "withdraw_sent"

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{fixture['buyer_user_id']}:buyer_available",),
            )
            buyer_balance = await cur.fetchone()
            assert buyer_balance["current_balance_usdt"] == Decimal("5.000000")


@pytest.mark.asyncio
async def test_purchase_lifecycle_delete_announcement_pays_active_purchase_and_returns_unassigned_collateral(
    db_pool,
) -> None:
    fixture = await _setup_active_announcement(
        db_pool,
        seller_telegram_id=623001,
        buyer_telegram_id=623002,
        wb_product_id=623003,
        reward_usdt=Decimal("5.000000"),
        slot_count=2,
    )
    lifecycle = PurchaseLifecycleService(db_pool)
    reservation = await lifecycle.reserve_purchase(
        buyer_user_id=fixture["buyer_user_id"],
        announcement_id=fixture["listing_id"],
        idempotency_seed="purchase-delete-reserve",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                result = await lifecycle.delete_announcement_locked(
                    cur,
                    seller_user_id=fixture["seller_user_id"],
                    announcement_id=fixture["listing_id"],
                    deleted_by_user_id=fixture["seller_user_id"],
                    idempotency_seed="purchase-delete-announcement",
                )

    assert result.changed is True
    assert result.assignment_transfers_count == 1
    assert result.assignment_transferred_usdt == Decimal("5.000000")
    assert result.unassigned_collateral_returned_usdt == Decimal("5.000000")

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (reservation.purchase_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "withdraw_sent"

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE account_code = %s",
                (f"user:{fixture['buyer_user_id']}:buyer_available",),
            )
            buyer_balance = await cur.fetchone()
            assert buyer_balance["current_balance_usdt"] == Decimal("5.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["seller_collateral_account_id"],),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("0.000000")
