from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.buyer import BuyerService
from libs.domain.errors import InvalidStateError, NoSlotsAvailableError
from libs.domain.ledger import FinanceService
from tests.helpers import create_account, create_listing, create_shop, create_user


@pytest.mark.asyncio
async def test_single_slot_allows_only_one_concurrent_reservation(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(conn, telegram_id=1001, role="seller", username="seller")
            buyer_one_id = await create_user(
                conn,
                telegram_id=2001,
                role="buyer",
                username="buyer1",
            )
            buyer_two_id = await create_user(
                conn,
                telegram_id=2002,
                role="buyer",
                username="buyer2",
            )

            seller_available_account_id = await create_account(
                conn,
                owner_user_id=seller_id,
                account_code="acct-seller-available",
                account_kind="seller_available",
                balance=Decimal("20.000000"),
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_id,
                account_code="acct-seller-collateral",
                account_kind="seller_collateral",
                balance=Decimal("0.000000"),
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="acct-reward-reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )

            shop_id = await create_shop(
                conn,
                seller_user_id=seller_id,
                slug="test-shop",
                title="Test",
            )
            listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_id,
                wb_product_id=555,
                reward_usdt=Decimal("10.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    await service.lock_listing_collateral(
        listing_id=listing_id,
        seller_available_account_id=seller_available_account_id,
        seller_collateral_account_id=seller_collateral_account_id,
        amount_usdt=Decimal("10.000000"),
        idempotency_key="lock-listing-1",
    )

    async def attempt_reserve(buyer_user_id: int, key: str) -> str:
        try:
            await service.create_assignment_reservation(
                listing_id=listing_id,
                buyer_user_id=buyer_user_id,
                seller_collateral_account_id=seller_collateral_account_id,
                reward_reserved_account_id=reward_reserved_account_id,
                idempotency_key=key,
            )
            return "success"
        except NoSlotsAvailableError:
            return "no_slots"

    outcomes = await asyncio.gather(
        attempt_reserve(buyer_one_id, "reserve-buyer-1"),
        attempt_reserve(buyer_two_id, "reserve-buyer-2"),
    )

    assert outcomes.count("success") == 1
    assert outcomes.count("no_slots") == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT available_slots FROM listings WHERE id = %s", (listing_id,))
            listing = await cur.fetchone()
            assert listing["available_slots"] == 0

            await cur.execute(
                "SELECT COUNT(*) AS count FROM assignments WHERE listing_id = %s",
                (listing_id,),
            )
            assignments_count = await cur.fetchone()
            assert assignments_count["count"] == 1

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (reward_reserved_account_id,),
            )
            reserved_balance = await cur.fetchone()
            assert reserved_balance["current_balance_usdt"] == Decimal("10.000000")


@pytest.mark.asyncio
async def test_same_buyer_cannot_get_two_active_assignments_for_same_product_concurrently(
    db_pool,
) -> None:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=1101,
                role="seller",
                username="seller_same_product",
            )
            buyer_id = await create_user(
                conn,
                telegram_id=2101,
                role="buyer",
                username="buyer_same_product",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_id,
                slug="same-product-shop",
                title="Same Product Shop",
            )
            listing_one_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_id,
                wb_product_id=990001,
                reward_usdt=Decimal("5.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )
            listing_two_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_id,
                wb_product_id=990001,
                reward_usdt=Decimal("5.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )
            await create_account(
                conn,
                owner_user_id=seller_id,
                account_code=f"user:{seller_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=Decimal("20.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )

    async def attempt(listing_id: int, key: str) -> str:
        try:
            await buyer_service.reserve_listing_slot(
                buyer_user_id=buyer_id,
                listing_id=listing_id,
                idempotency_key=key,
            )
            return "success"
        except InvalidStateError as exc:
            if "already has assignment" in str(exc):
                return "duplicate_blocked"
            raise

    outcomes = await asyncio.gather(
        attempt(listing_one_id, "reserve:same-product:1"),
        attempt(listing_two_id, "reserve:same-product:2"),
    )

    assert outcomes.count("success") == 1
    assert outcomes.count("duplicate_blocked") == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM assignments
                WHERE buyer_user_id = %s
                  AND wb_product_id = %s
                  AND status = 'reserved'
                """,
                (buyer_id, 990001),
            )
            assignments_count = await cur.fetchone()
            assert assignments_count["count"] == 1

            await cur.execute(
                """
                SELECT SUM(available_slots) AS total_slots
                FROM listings
                WHERE id = ANY(%s)
                """,
                ([listing_one_id, listing_two_id],),
            )
            slots_row = await cur.fetchone()
            assert slots_row["total_slots"] == 1
