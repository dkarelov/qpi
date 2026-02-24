from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.ledger import FinanceService
from tests.helpers import create_account, create_listing, create_shop, create_user


@pytest.mark.asyncio
async def test_ledger_flows_keep_global_balance_and_double_entry_invariant(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(conn, telegram_id=3001, role="seller", username="seller")
            buyer_id = await create_user(conn, telegram_id=3002, role="buyer", username="buyer")
            admin_id = await create_user(conn, telegram_id=3003, role="admin", username="admin")

            seller_available_account_id = await create_account(
                conn,
                owner_user_id=seller_id,
                account_code="acct2-seller-available",
                account_kind="seller_available",
                balance=Decimal("100.000000"),
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_id,
                account_code="acct2-seller-collateral",
                account_kind="seller_collateral",
                balance=Decimal("0.000000"),
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="acct2-reward-reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code="acct2-buyer-available",
                account_kind="buyer_available",
                balance=Decimal("0.000000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code="acct2-buyer-pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )
            system_payout_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="acct2-system-payout",
                account_kind="system_payout",
                balance=Decimal("0.000000"),
            )

            shop_id = await create_shop(
                conn,
                seller_user_id=seller_id,
                slug="shop-two",
                title="Shop Two",
            )
            listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_id,
                wb_product_id=777,
                reward_usdt=Decimal("10.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    first_lock = await service.lock_listing_collateral(
        listing_id=listing_id,
        seller_available_account_id=seller_available_account_id,
        seller_collateral_account_id=seller_collateral_account_id,
        amount_usdt=Decimal("10.000000"),
        idempotency_key="lock-2",
    )
    second_lock = await service.lock_listing_collateral(
        listing_id=listing_id,
        seller_available_account_id=seller_available_account_id,
        seller_collateral_account_id=seller_collateral_account_id,
        amount_usdt=Decimal("10.000000"),
        idempotency_key="lock-2",
    )

    assert first_lock.created is True
    assert second_lock.created is False

    reservation = await service.create_assignment_reservation(
        listing_id=listing_id,
        buyer_user_id=buyer_id,
        seller_collateral_account_id=seller_collateral_account_id,
        reward_reserved_account_id=reward_reserved_account_id,
        idempotency_key="reserve-2",
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
                    (reservation.assignment_id,),
                )

    unlock = await service.unlock_assignment_reward(
        assignment_id=reservation.assignment_id,
        buyer_available_account_id=buyer_available_account_id,
        reward_reserved_account_id=reward_reserved_account_id,
        idempotency_key="unlock-2",
    )
    assert unlock.changed is True

    withdrawal = await service.create_withdrawal_request(
        buyer_user_id=buyer_id,
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("10.000000"),
        payout_address="UQ_TEST_ADDRESS",
        idempotency_key="withdraw-request-2",
    )
    assert withdrawal.created is True

    approved = await service.approve_withdrawal_request(
        request_id=withdrawal.withdrawal_request_id,
        admin_user_id=admin_id,
        idempotency_key="withdraw-approve-2",
    )
    assert approved.changed is True

    sent = await service.mark_withdrawal_sent(
        request_id=withdrawal.withdrawal_request_id,
        admin_user_id=admin_id,
        pending_account_id=buyer_pending_account_id,
        system_payout_account_id=system_payout_account_id,
        tx_hash="0xabc123",
        idempotency_key="withdraw-send-2",
    )
    assert sent.changed is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COALESCE(
                    SUM(
                        CASE
                            WHEN direction = 'credit' THEN amount_usdt
                            WHEN direction = 'debit' THEN -amount_usdt
                        END
                    ),
                    0
                ) AS imbalance
                FROM ledger_postings
                """
            )
            imbalance = await cur.fetchone()
            assert imbalance["imbalance"] == Decimal("0.000000")

            await cur.execute(
                "SELECT SUM(current_balance_usdt) AS total_balance FROM accounts"
            )
            total_balance = await cur.fetchone()
            assert total_balance["total_balance"] == Decimal("100.000000")

            await cur.execute(
                "SELECT COUNT(*) AS count FROM accounts WHERE current_balance_usdt < 0"
            )
            negative_accounts = await cur.fetchone()
            assert negative_accounts["count"] == 0

            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (reservation.assignment_id,),
            )
            assignment_row = await cur.fetchone()
            assert assignment_row["status"] == "eligible_for_withdrawal"

            await cur.execute(
                "SELECT status FROM withdrawal_requests WHERE id = %s",
                (withdrawal.withdrawal_request_id,),
            )
            withdrawal_row = await cur.fetchone()
            assert withdrawal_row["status"] == "withdraw_sent"
