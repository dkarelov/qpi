from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.errors import InvalidStateError
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

    sent = await service.complete_withdrawal_request(
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
            assert assignment_row["status"] == "withdraw_sent"

            await cur.execute(
                "SELECT status FROM withdrawal_requests WHERE id = %s",
                (withdrawal.withdrawal_request_id,),
            )
            withdrawal_row = await cur.fetchone()
            assert withdrawal_row["status"] == "withdraw_sent"


@pytest.mark.asyncio
async def test_buyer_balance_and_withdraw_history_queries(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(conn, telegram_id=3901, role="buyer", username="buyer_q")
            await create_user(conn, telegram_id=3902, role="admin", username="admin_q")

            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("12.500000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    request = await service.create_withdrawal_request(
        buyer_user_id=buyer_id,
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("2.500000"),
        payout_address="UQ_BALANCE_TEST",
        idempotency_key="withdraw-history-1",
    )

    snapshot = await service.get_buyer_balance_snapshot(buyer_user_id=buyer_id)
    assert snapshot.buyer_available_usdt == Decimal("10.000000")
    assert snapshot.buyer_withdraw_pending_usdt == Decimal("2.500000")

    history = await service.list_buyer_withdrawal_history(buyer_user_id=buyer_id)
    assert len(history) == 1
    assert history[0].withdrawal_request_id == request.withdrawal_request_id
    assert history[0].amount_usdt == Decimal("2.500000")
    assert history[0].status == "withdraw_pending_admin"

    pending = await service.list_pending_withdrawals()
    assert len(pending) == 1
    assert pending[0].withdrawal_request_id == request.withdrawal_request_id
    assert pending[0].buyer_user_id == buyer_id

    detail = await service.get_withdrawal_request_detail(request_id=request.withdrawal_request_id)
    assert detail.withdrawal_request_id == request.withdrawal_request_id
    assert detail.buyer_user_id == buyer_id
    assert detail.from_account_id == buyer_available_account_id
    assert detail.to_account_id == buyer_pending_account_id


@pytest.mark.asyncio
async def test_reject_withdrawal_persists_reason_note(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=3911,
                role="buyer",
                username="buyer_note",
            )
            admin_id = await create_user(
                conn,
                telegram_id=3912,
                role="admin",
                username="admin_note",
            )
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

    request = await service.create_withdrawal_request(
        buyer_user_id=buyer_id,
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.000000"),
        payout_address="UQ_NOTE_TEST",
        idempotency_key="withdraw-note-1",
    )

    result = await service.reject_withdrawal_request(
        request_id=request.withdrawal_request_id,
        admin_user_id=admin_id,
        pending_account_id=buyer_pending_account_id,
        buyer_available_account_id=buyer_available_account_id,
        reason="invalid payout address",
        idempotency_key="withdraw-note-reject-1",
    )
    assert result.changed is True

    detail = await service.get_withdrawal_request_detail(request_id=request.withdrawal_request_id)
    assert detail.status == "rejected"
    assert detail.note == "invalid payout address"


@pytest.mark.asyncio
async def test_buyer_cancel_withdrawal_returns_funds_and_marks_cancelled(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=3921,
                role="buyer",
                username="buyer_cancel",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("3.000000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    request = await service.create_withdrawal_request(
        buyer_user_id=buyer_id,
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.500000"),
        payout_address="UQ_CANCEL_TEST",
        idempotency_key="withdraw-cancel-1",
    )

    cancelled = await service.cancel_withdrawal_request(
        request_id=request.withdrawal_request_id,
        buyer_user_id=buyer_id,
        idempotency_key="withdraw-cancel-1:cancel",
    )
    assert cancelled.changed is True

    snapshot = await service.get_buyer_balance_snapshot(buyer_user_id=buyer_id)
    assert snapshot.buyer_available_usdt == Decimal("3.000000")
    assert snapshot.buyer_withdraw_pending_usdt == Decimal("0.000000")

    detail = await service.get_withdrawal_request_detail(request_id=request.withdrawal_request_id)
    assert detail.status == "cancelled"


@pytest.mark.asyncio
async def test_buyer_cannot_create_second_active_withdrawal_request(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=3922,
                role="buyer",
                username="buyer_active_once",
            )
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

    await service.create_withdrawal_request(
        buyer_user_id=buyer_id,
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.000000"),
        payout_address="UQ_ACTIVE_1",
        idempotency_key="withdraw-active-1",
    )

    with pytest.raises(InvalidStateError, match="active withdrawal request"):
        await service.create_withdrawal_request(
            buyer_user_id=buyer_id,
            from_account_id=buyer_available_account_id,
            pending_account_id=buyer_pending_account_id,
            amount_usdt=Decimal("1.000000"),
            payout_address="UQ_ACTIVE_2",
            idempotency_key="withdraw-active-2",
        )


@pytest.mark.asyncio
async def test_manual_deposit_credit_is_idempotent_and_audited(db_pool) -> None:
    service = FinanceService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            target_user_id = await create_user(
                conn,
                telegram_id=3921,
                role="seller",
                username="seller_deposit",
            )
            admin_user_id = await create_user(
                conn,
                telegram_id=3922,
                role="admin",
                username="admin_deposit",
            )
            target_account_id = await create_account(
                conn,
                owner_user_id=target_user_id,
                account_code=f"user:{target_user_id}:seller_available",
                account_kind="seller_available",
                balance=Decimal("0.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("20.000000"),
            )

    first = await service.manual_deposit_credit(
        admin_user_id=admin_user_id,
        target_user_id=target_user_id,
        target_account_id=target_account_id,
        amount_usdt=Decimal("4.000000"),
        external_reference="deposit-tx-1",
        idempotency_key="manual-deposit-1",
        tx_hash="0xdeposit1",
    )
    second = await service.manual_deposit_credit(
        admin_user_id=admin_user_id,
        target_user_id=target_user_id,
        target_account_id=target_account_id,
        amount_usdt=Decimal("4.000000"),
        external_reference="deposit-tx-1",
        idempotency_key="manual-deposit-1",
        tx_hash="0xdeposit1",
    )

    assert first.created is True
    assert second.created is False
    assert first.manual_deposit_id == second.manual_deposit_id
    assert first.ledger_entry_id == second.ledger_entry_id

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (target_account_id,),
            )
            target_balance = await cur.fetchone()
            assert target_balance["current_balance_usdt"] == Decimal("4.000000")

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM manual_deposits
                WHERE id = %s
                """,
                (first.manual_deposit_id,),
            )
            deposits_count = await cur.fetchone()
            assert deposits_count["count"] == 1

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM admin_audit_actions
                WHERE action = 'manual_deposit_credit'
                """,
            )
            audit_count = await cur.fetchone()
            assert audit_count["count"] == 1
