from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.errors import NoSlotsAvailableError
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
