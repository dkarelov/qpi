from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.blockchain_checker import BlockchainCheckerService
from libs.domain.deposit_intents import DepositIntentService
from libs.domain.errors import InvalidStateError
from libs.domain.ledger import FinanceService
from libs.integrations.tonapi import (
    TonapiAddressInfo,
    TonapiJettonHistoryPage,
    TonapiJettonOperation,
)
from tests.helpers import create_account, create_user


class StubTonapiClient:
    def __init__(
        self,
        operations: list[TonapiJettonOperation],
        *,
        shard_raw: str,
        parsed_addresses: dict[str, str] | None = None,
    ):
        self._operations = operations
        self._shard_raw = shard_raw
        self._parsed_addresses = parsed_addresses or {}
        self.parse_calls: list[str] = []
        self.history_calls: list[int | None] = []

    async def parse_address(self, *, account_id: str) -> TonapiAddressInfo:
        self.parse_calls.append(account_id)
        return TonapiAddressInfo(raw_form=self._parsed_addresses.get(account_id, self._shard_raw))

    async def get_jetton_account_history(
        self,
        *,
        account_id: str,
        jetton_id: str,
        limit: int,
        before_lt: int | None = None,
    ) -> TonapiJettonHistoryPage:
        self.history_calls.append(before_lt)
        return TonapiJettonHistoryPage(operations=list(self._operations), next_from=None)


class StubTonapiPagedClient:
    def __init__(
        self,
        pages_by_before_lt: dict[int | None, TonapiJettonHistoryPage],
        *,
        shard_raw: str,
        parsed_addresses: dict[str, str] | None = None,
    ):
        self._pages_by_before_lt = pages_by_before_lt
        self._shard_raw = shard_raw
        self._parsed_addresses = parsed_addresses or {}
        self.parse_calls: list[str] = []
        self.history_calls: list[int | None] = []

    async def parse_address(self, *, account_id: str) -> TonapiAddressInfo:
        self.parse_calls.append(account_id)
        return TonapiAddressInfo(raw_form=self._parsed_addresses.get(account_id, self._shard_raw))

    async def get_jetton_account_history(
        self,
        *,
        account_id: str,
        jetton_id: str,
        limit: int,
        before_lt: int | None = None,
    ) -> TonapiJettonHistoryPage:
        self.history_calls.append(before_lt)
        return self._pages_by_before_lt.get(
            before_lt,
            TonapiJettonHistoryPage(operations=[], next_from=None),
        )


class RecordingLogger:
    def __init__(self) -> None:
        self.info_events: list[tuple[object, dict[str, object]]] = []
        self.warning_events: list[tuple[object, dict[str, object]]] = []

    def info(self, event: object, **fields: object) -> None:
        self.info_events.append((event, fields))

    def warning(self, event: object, **fields: object) -> None:
        self.warning_events.append((event, fields))


def _op(
    *,
    tx_hash: str,
    lt: int,
    query_id: str,
    trace_id: str,
    amount_raw: str,
    utime: datetime,
    src: str,
    dst: str,
) -> TonapiJettonOperation:
    return TonapiJettonOperation(
        operation="transfer",
        utime=utime,
        lt=lt,
        transaction_hash=tx_hash,
        source_address=src,
        destination_address=dst,
        amount_raw=amount_raw,
        decimals=6,
        query_id=query_id,
        trace_id=trace_id,
        payload={"tx_hash": tx_hash},
    )


@pytest.mark.asyncio
async def test_deposit_intent_rounding_suffix_and_idempotency(db_pool) -> None:
    service = DepositIntentService(db_pool)
    shard = await service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=930001,
                role="seller",
                username="seller930001",
            )

    first = await service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:1",
    )
    replay = await service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:1",
    )
    second = await service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:2",
    )

    assert first.created is True
    assert first.base_amount_usdt == Decimal("1.300000")
    assert first.expected_amount_usdt == Decimal("1.300100")
    assert first.suffix_code == 1

    assert replay.created is False
    assert replay.deposit_intent_id == first.deposit_intent_id

    assert second.created is True
    assert second.suffix_code == 2
    assert second.expected_amount_usdt == Decimal("1.300200")


@pytest.mark.asyncio
async def test_admin_can_create_seller_deposit_intent_for_0_01_amount(db_pool) -> None:
    service = DepositIntentService(db_pool)
    shard = await service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            admin_id = await create_user(
                conn,
                telegram_id=930010,
                role="admin",
                username="admin930010",
            )

    intent = await service.create_seller_deposit_intent(
        seller_user_id=admin_id,
        request_amount_usdt=Decimal("0.01"),
        shard_id=shard.shard_id,
        idempotency_key="intent:admin:0.01",
    )

    assert intent.created is True
    assert intent.base_amount_usdt == Decimal("0.100000")
    assert intent.expected_amount_usdt == Decimal("0.100100")
    assert intent.suffix_code == 1


@pytest.mark.asyncio
async def test_chain_tx_upsert_accepts_json_payload_dict(db_pool) -> None:
    service = DepositIntentService(db_pool)
    shard = await service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    now = datetime.now(UTC)
    payload = {"source": "tonapi", "query_id": ""}
    result = await service.upsert_chain_incoming_tx(
        shard_id=shard.shard_id,
        provider="tonapi",
        chain="ton_mainnet",
        asset="USDT",
        tx_hash="tx-json-payload-1",
        tx_lt=9001,
        query_id="",
        trace_id="trace-json-payload-1",
        operation_type="transfer",
        source_address="0:source",
        destination_address="0:shard",
        amount_raw="1000200",
        amount_usdt=Decimal("1.000200"),
        occurred_at=now,
        raw_payload_json=payload,
    )
    assert result.created is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT raw_payload_json
                FROM chain_incoming_txs
                WHERE id = %s
                """,
                (result.chain_tx_id,),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["raw_payload_json"] == payload


@pytest.mark.asyncio
async def test_deposit_intent_suffix_pool_exhaustion(db_pool) -> None:
    service = DepositIntentService(db_pool)
    shard = await service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=930002,
                role="seller",
                username="seller930002",
            )

    for index in range(1, 1000):
        intent = await service.create_seller_deposit_intent(
            seller_user_id=seller_id,
            request_amount_usdt=Decimal("1.0"),
            shard_id=shard.shard_id,
            idempotency_key=f"intent:{index}",
        )
        assert intent.suffix_code == index

    with pytest.raises(InvalidStateError):
        await service.create_seller_deposit_intent(
            seller_user_id=seller_id,
            request_amount_usdt=Decimal("1.0"),
            shard_id=shard.shard_id,
            idempotency_key="intent:overflow",
        )


@pytest.mark.asyncio
async def test_blockchain_checker_happy_path_and_idempotency(
    db_pool,
    isolated_database: str,
) -> None:
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=930003,
                role="seller",
                username="seller930003",
            )
            await create_account(
                conn,
                owner_user_id=seller_id,
                account_code=f"user:{seller_id}:seller_available",
                account_kind="seller_available",
                balance=Decimal("0.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("1000.000000"),
            )

    intent = await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:happy",
    )

    now = datetime.now(UTC)
    expected_raw = "1300100"
    op = _op(
        tx_hash="tx-happy-1",
        lt=101,
        query_id="q1",
        trace_id="t1",
        amount_raw=expected_raw,
        utime=now,
        src="0:source",
        dst="0:shard",
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008001,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=StubTonapiClient([op], shard_raw="0:shard"),
        deposit_service=deposit_service,
    )

    first = await checker.run_once()
    second = await checker.run_once()

    assert first.tx_credited_count == 1
    assert first.tx_manual_review_count == 0
    assert second.tx_credited_count == 0

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{seller_id}:seller_available",),
            )
            seller_balance = await cur.fetchone()
            assert seller_balance["current_balance_usdt"] == Decimal("1.300100")

            await cur.execute(
                """
                SELECT status, credited_amount_usdt
                FROM deposit_intents
                WHERE id = %s
                """,
                (intent.deposit_intent_id,),
            )
            intent_row = await cur.fetchone()
            assert intent_row["status"] == "credited"
            assert intent_row["credited_amount_usdt"] == Decimal("1.300100")

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM system_balance_provisions
                WHERE event_type = 'expected_deposit_credit'
                """
            )
            provision_row = await cur.fetchone()
            assert provision_row["count"] == 1


@pytest.mark.asyncio
async def test_blockchain_checker_auto_completes_matching_withdrawal_payout(
    db_pool,
    isolated_database: str,
) -> None:
    finance_service = FinanceService(db_pool)
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQ_PAYOUT_WALLET",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=930020,
                role="buyer",
                username="buyer930020",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("1.342102"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("0.000000"),
            )

    withdrawal = await finance_service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.342102"),
        payout_address="UQ_BUYER_WALLET",
        idempotency_key="withdrawal:auto-complete",
    )

    now = datetime.now(UTC)
    old_matching_op = _op(
        tx_hash="tx-withdrawal-old",
        lt=900,
        query_id="q-withdrawal-old",
        trace_id="t-withdrawal-old",
        amount_raw="1342102",
        utime=now - timedelta(days=1),
        src="0:payout-wallet",
        dst="0:buyer-wallet",
    )
    matching_op = _op(
        tx_hash="tx-withdrawal-complete",
        lt=901,
        query_id="q-withdrawal-complete",
        trace_id="t-withdrawal-complete",
        amount_raw="1342102",
        utime=now + timedelta(seconds=1),
        src="0:payout-wallet",
        dst="0:buyer-wallet",
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008020,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=StubTonapiClient(
            [matching_op, old_matching_op],
            shard_raw="0:payout-wallet",
            parsed_addresses={
                shard.deposit_address: "0:payout-wallet",
                "UQ_BUYER_WALLET": "0:buyer-wallet",
            },
        ),
        deposit_service=deposit_service,
        finance_service=finance_service,
    )

    first = await checker.run_once()
    second = await checker.run_once()

    assert first.withdrawals_completed_count == 1
    assert first.withdrawals_ambiguous_count == 0
    assert second.withdrawals_completed_count == 0

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, admin_user_id, processed_at, sent_at
                FROM withdrawal_requests
                WHERE id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            request_row = await cur.fetchone()
            assert request_row["status"] == "withdraw_sent"
            assert request_row["admin_user_id"] is None
            assert request_row["processed_at"] is not None
            assert request_row["sent_at"] is not None

            await cur.execute(
                """
                SELECT tx_hash, status
                FROM payouts
                WHERE withdrawal_request_id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            payout_row = await cur.fetchone()
            assert payout_row["tx_hash"] == "tx-withdrawal-complete"
            assert payout_row["status"] == "sent"

            await cur.execute(
                """
                SELECT account_kind, current_balance_usdt
                FROM accounts
                WHERE id IN (%s, %s)
                   OR account_code = 'system:system_payout'
                """,
                (buyer_available_account_id, buyer_pending_account_id),
            )
            balances = {row["account_kind"]: row["current_balance_usdt"] for row in await cur.fetchall()}
            assert balances["buyer_available"] == Decimal("0.000000")
            assert balances["buyer_withdraw_pending"] == Decimal("0.000000")
            assert balances["system_payout"] == Decimal("1.342102")

            await cur.execute(
                """
                SELECT status
                FROM balance_holds
                WHERE withdrawal_request_id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            hold_row = await cur.fetchone()
            assert hold_row["status"] == "consumed"

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM admin_audit_actions
                WHERE target_type = 'withdrawal_request'
                  AND target_id = %s
                """,
                (str(withdrawal.withdrawal_request_id),),
            )
            audit_count = await cur.fetchone()
            assert audit_count["count"] == 0


@pytest.mark.asyncio
async def test_blockchain_checker_dedupes_overlapping_withdrawal_payout_pages(
    db_pool,
    isolated_database: str,
) -> None:
    finance_service = FinanceService(db_pool)
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQ_PAYOUT_WALLET",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=930040,
                role="buyer",
                username="buyer930040",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("1.342102"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    withdrawal = await finance_service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.342102"),
        payout_address="UQ_DUP_BUYER_WALLET",
        idempotency_key="withdrawal:overlapping-pages",
    )

    matching_op = _op(
        tx_hash="tx-withdrawal-overlap",
        lt=920,
        query_id="q-withdrawal-overlap",
        trace_id="t-withdrawal-overlap",
        amount_raw="1342102",
        utime=datetime.now(UTC) + timedelta(seconds=1),
        src="0:payout-wallet",
        dst="0:buyer-wallet",
    )
    paged_client = StubTonapiPagedClient(
        {
            None: TonapiJettonHistoryPage(operations=[matching_op], next_from=910),
            910: TonapiJettonHistoryPage(operations=[matching_op], next_from=None),
        },
        shard_raw="0:payout-wallet",
        parsed_addresses={
            shard.deposit_address: "0:payout-wallet",
            "UQ_DUP_BUYER_WALLET": "0:buyer-wallet",
        },
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008022,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=paged_client,
        deposit_service=deposit_service,
        finance_service=finance_service,
    )

    result = await checker.run_once()

    assert result.withdrawals_completed_count == 1
    assert result.withdrawals_ambiguous_count == 0
    assert paged_client.history_calls == [None, 910]
    assert paged_client.parse_calls.count(shard.deposit_address) == 1
    assert paged_client.parse_calls.count("UQ_DUP_BUYER_WALLET") == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM withdrawal_requests
                WHERE id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            request_row = await cur.fetchone()
            assert request_row["status"] == "withdraw_sent"

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM payouts
                WHERE withdrawal_request_id = %s
                  AND tx_hash = 'tx-withdrawal-overlap'
                """,
                (withdrawal.withdrawal_request_id,),
            )
            payout_count = await cur.fetchone()
            assert payout_count["count"] == 1


@pytest.mark.asyncio
async def test_blockchain_checker_fetches_payout_history_from_head_when_ingest_resumes_cursor(
    db_pool,
    isolated_database: str,
) -> None:
    finance_service = FinanceService(db_pool)
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQ_PAYOUT_WALLET",
    )
    usdt_master = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    await deposit_service.set_scan_cursor(
        source_key=f"tonapi:{shard.shard_id}:{usdt_master}",
        last_lt=1000,
        resume_before_lt=500,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=930042,
                role="buyer",
                username="buyer930042",
            )
            buyer_available_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_available",
                account_kind="buyer_available",
                balance=Decimal("1.500000"),
            )
            buyer_pending_account_id = await create_account(
                conn,
                owner_user_id=buyer_id,
                account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                account_kind="buyer_withdraw_pending",
                balance=Decimal("0.000000"),
            )

    withdrawal = await finance_service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("1.500000"),
        payout_address="UQ_RESUME_BUYER_WALLET",
        idempotency_key="withdrawal:resume-cursor",
    )

    old_resume_op = _op(
        tx_hash="tx-withdrawal-resume-old",
        lt=490,
        query_id="q-withdrawal-resume-old",
        trace_id="t-withdrawal-resume-old",
        amount_raw="1000000",
        utime=datetime.now(UTC) - timedelta(hours=1),
        src="0:payout-wallet",
        dst="0:other-wallet",
    )
    matching_op = _op(
        tx_hash="tx-withdrawal-resume-complete",
        lt=1100,
        query_id="q-withdrawal-resume-complete",
        trace_id="t-withdrawal-resume-complete",
        amount_raw="1500000",
        utime=datetime.now(UTC) + timedelta(seconds=1),
        src="0:payout-wallet",
        dst="0:resume-buyer-wallet",
    )
    paged_client = StubTonapiPagedClient(
        {
            500: TonapiJettonHistoryPage(operations=[old_resume_op], next_from=None),
            None: TonapiJettonHistoryPage(operations=[matching_op], next_from=None),
        },
        shard_raw="0:payout-wallet",
        parsed_addresses={
            shard.deposit_address: "0:payout-wallet",
            "UQ_RESUME_BUYER_WALLET": "0:resume-buyer-wallet",
        },
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008024,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master=usdt_master,
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=paged_client,
        deposit_service=deposit_service,
        finance_service=finance_service,
    )

    result = await checker.run_once()

    assert result.withdrawals_completed_count == 1
    assert paged_client.history_calls == [500, None]

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM withdrawal_requests
                WHERE id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            request_row = await cur.fetchone()
            assert request_row["status"] == "withdraw_sent"


@pytest.mark.asyncio
async def test_blockchain_checker_warns_when_withdrawal_payout_scan_hits_page_cap(
    db_pool,
    isolated_database: str,
) -> None:
    finance_service = FinanceService(db_pool)
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQ_PAYOUT_WALLET",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_id = await create_user(
                conn,
                telegram_id=930041,
                role="buyer",
                username="buyer930041",
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

    withdrawal = await finance_service.create_withdrawal_request(
        requester_user_id=buyer_id,
        requester_role="buyer",
        from_account_id=buyer_available_account_id,
        pending_account_id=buyer_pending_account_id,
        amount_usdt=Decimal("3.000000"),
        payout_address="UQ_CAPPED_BUYER_WALLET",
        idempotency_key="withdrawal:payout-page-cap",
    )

    page_cap_op = _op(
        tx_hash="tx-withdrawal-page-cap-unrelated",
        lt=930,
        query_id="q-withdrawal-page-cap",
        trace_id="t-withdrawal-page-cap",
        amount_raw="1000000",
        utime=datetime.now(UTC) + timedelta(seconds=1),
        src="0:payout-wallet",
        dst="0:other-wallet",
    )
    paged_client = StubTonapiPagedClient(
        {
            None: TonapiJettonHistoryPage(operations=[page_cap_op], next_from=920),
        },
        shard_raw="0:payout-wallet",
        parsed_addresses={
            shard.deposit_address: "0:payout-wallet",
            "UQ_CAPPED_BUYER_WALLET": "0:capped-buyer-wallet",
        },
    )
    logger = RecordingLogger()

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008023,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=1,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=paged_client,
        deposit_service=deposit_service,
        finance_service=finance_service,
        logger=logger,
    )

    result = await checker.run_once()

    assert result.withdrawals_completed_count == 0
    assert result.withdrawals_ambiguous_count == 0
    assert paged_client.history_calls == [None]
    assert any(
        event == "blockchain_checker_withdrawal_scan_incomplete_page_cap"
        for event, _fields in logger.warning_events
    )

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM withdrawal_requests
                WHERE id = %s
                """,
                (withdrawal.withdrawal_request_id,),
            )
            request_row = await cur.fetchone()
            assert request_row["status"] == "withdraw_pending_admin"

            await cur.execute("SELECT COUNT(*) AS count FROM payouts")
            payout_count = await cur.fetchone()
            assert payout_count["count"] == 0


@pytest.mark.asyncio
async def test_blockchain_checker_keeps_ambiguous_withdrawal_payout_pending(
    db_pool,
    isolated_database: str,
) -> None:
    finance_service = FinanceService(db_pool)
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQ_PAYOUT_WALLET",
    )

    withdrawals = []
    async with db_pool.connection() as conn:
        async with conn.transaction():
            for index in range(2):
                buyer_id = await create_user(
                    conn,
                    telegram_id=930030 + index,
                    role="buyer",
                    username=f"buyer93003{index}",
                )
                buyer_available_account_id = await create_account(
                    conn,
                    owner_user_id=buyer_id,
                    account_code=f"user:{buyer_id}:buyer_available",
                    account_kind="buyer_available",
                    balance=Decimal("2.000000"),
                )
                buyer_pending_account_id = await create_account(
                    conn,
                    owner_user_id=buyer_id,
                    account_code=f"user:{buyer_id}:buyer_withdraw_pending",
                    account_kind="buyer_withdraw_pending",
                    balance=Decimal("0.000000"),
                )
                withdrawals.append(
                    (
                        buyer_id,
                        buyer_available_account_id,
                        buyer_pending_account_id,
                    )
                )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("0.000000"),
            )

    withdrawal_ids = []
    for index, (buyer_id, buyer_available_account_id, buyer_pending_account_id) in enumerate(withdrawals):
        request = await finance_service.create_withdrawal_request(
            requester_user_id=buyer_id,
            requester_role="buyer",
            from_account_id=buyer_available_account_id,
            pending_account_id=buyer_pending_account_id,
            amount_usdt=Decimal("2.000000"),
            payout_address="UQ_SHARED_BUYER_WALLET",
            idempotency_key=f"withdrawal:ambiguous:{index}",
        )
        withdrawal_ids.append(request.withdrawal_request_id)

    ambiguous_op = _op(
        tx_hash="tx-withdrawal-ambiguous",
        lt=910,
        query_id="q-withdrawal-ambiguous",
        trace_id="t-withdrawal-ambiguous",
        amount_raw="2000000",
        utime=datetime.now(UTC) + timedelta(seconds=1),
        src="0:payout-wallet",
        dst="0:shared-buyer-wallet",
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008021,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=StubTonapiClient(
            [ambiguous_op],
            shard_raw="0:payout-wallet",
            parsed_addresses={
                shard.deposit_address: "0:payout-wallet",
                "UQ_SHARED_BUYER_WALLET": "0:shared-buyer-wallet",
            },
        ),
        deposit_service=deposit_service,
        finance_service=finance_service,
    )

    result = await checker.run_once()

    assert result.withdrawals_completed_count == 0
    assert result.withdrawals_ambiguous_count == 2

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM withdrawal_requests
                WHERE id = ANY(%s)
                  AND status = 'withdraw_pending_admin'
                """,
                (withdrawal_ids,),
            )
            pending_count = await cur.fetchone()
            assert pending_count["count"] == 2

            await cur.execute("SELECT COUNT(*) AS count FROM payouts")
            payout_count = await cur.fetchone()
            assert payout_count["count"] == 0


@pytest.mark.asyncio
async def test_blockchain_checker_partial_and_late_go_to_manual_review(
    db_pool,
    isolated_database: str,
) -> None:
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=930004,
                role="seller",
                username="seller930004",
            )
            await create_account(
                conn,
                owner_user_id=seller_id,
                account_code=f"user:{seller_id}:seller_available",
                account_kind="seller_available",
                balance=Decimal("0.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("1000.000000"),
            )

    partial_intent = await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:partial",
    )
    late_intent = await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("2.34"),
        shard_id=shard.shard_id,
        idempotency_key="intent:late",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE deposit_intents
                    SET expires_at = timezone('utc', now()) - interval '1 minute'
                    WHERE id = %s
                    """,
                    (late_intent.deposit_intent_id,),
                )

    now = datetime.now(UTC)
    partial_op = _op(
        tx_hash="tx-partial-1",
        lt=201,
        query_id="qp1",
        trace_id="tp1",
        amount_raw="1200100",
        utime=now,
        src="0:source",
        dst="0:shard",
    )
    late_op = _op(
        tx_hash="tx-late-1",
        lt=202,
        query_id="ql1",
        trace_id="tl1",
        amount_raw="2400200",
        utime=now + timedelta(seconds=1),
        src="0:source",
        dst="0:shard",
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008002,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=StubTonapiClient([late_op, partial_op], shard_raw="0:shard"),
        deposit_service=deposit_service,
    )

    result = await checker.run_once()

    assert result.tx_credited_count == 0
    assert result.tx_manual_review_count == 2

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status
                FROM deposit_intents
                WHERE id = %s
                """,
                (partial_intent.deposit_intent_id,),
            )
            partial_row = await cur.fetchone()
            assert partial_row["status"] == "manual_review"

            await cur.execute(
                """
                SELECT status
                FROM deposit_intents
                WHERE id = %s
                """,
                (late_intent.deposit_intent_id,),
            )
            late_row = await cur.fetchone()
            assert late_row["status"] == "expired"

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM chain_incoming_txs
                WHERE status = 'manual_review'
                """
            )
            tx_count = await cur.fetchone()
            assert tx_count["count"] == 2


@pytest.mark.asyncio
async def test_blockchain_checker_does_not_credit_pre_invoice_payment_to_reused_suffix(
    db_pool,
    isolated_database: str,
) -> None:
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_one_id = await create_user(
                conn,
                telegram_id=930014,
                role="seller",
                username="seller930014",
            )
            seller_two_id = await create_user(
                conn,
                telegram_id=930015,
                role="seller",
                username="seller930015",
            )
            for seller_id in (seller_one_id, seller_two_id):
                await create_account(
                    conn,
                    owner_user_id=seller_id,
                    account_code=f"user:{seller_id}:seller_available",
                    account_kind="seller_available",
                    balance=Decimal("0.000000"),
                )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("1000.000000"),
            )

    old_intent = await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_one_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:old-reused-suffix",
    )
    now = datetime.now(UTC)
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE deposit_intents
                    SET created_at = %s,
                        expires_at = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (
                        now - timedelta(hours=3),
                        now - timedelta(hours=2),
                        old_intent.deposit_intent_id,
                    ),
                )

    new_intent = await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_two_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:new-reused-suffix",
    )
    assert new_intent.suffix_code == old_intent.suffix_code

    pre_invoice_op = _op(
        tx_hash="tx-reused-suffix-pre-invoice",
        lt=301,
        query_id="qr1",
        trace_id="tr1",
        amount_raw="1300100",
        utime=now - timedelta(hours=1),
        src="0:source",
        dst="0:shard",
    )
    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008014,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=5,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=StubTonapiClient([pre_invoice_op], shard_raw="0:shard"),
        deposit_service=deposit_service,
    )

    result = await checker.run_once()

    assert result.tx_credited_count == 0
    assert result.tx_manual_review_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{seller_two_id}:seller_available",),
            )
            seller_two_balance = await cur.fetchone()
            assert seller_two_balance["current_balance_usdt"] == Decimal("0.000000")

            await cur.execute(
                """
                SELECT status
                FROM deposit_intents
                WHERE id = %s
                """,
                (new_intent.deposit_intent_id,),
            )
            new_intent_row = await cur.fetchone()
            assert new_intent_row["status"] == "pending"

            await cur.execute(
                """
                SELECT status, matched_intent_id, review_reason
                FROM chain_incoming_txs
                WHERE tx_hash = 'tx-reused-suffix-pre-invoice'
                """
            )
            tx_row = await cur.fetchone()
            assert tx_row["status"] == "manual_review"
            assert tx_row["matched_intent_id"] == new_intent.deposit_intent_id
            assert tx_row["review_reason"] == "no_active_intent_for_suffix"


@pytest.mark.asyncio
async def test_blockchain_checker_does_not_advance_cursor_on_page_cap(
    db_pool,
    isolated_database: str,
) -> None:
    deposit_service = DepositIntentService(db_pool)
    shard = await deposit_service.ensure_default_shard(
        shard_key="mvp-1",
        deposit_address="UQBYf1gmISdOD-D2iAsxSZI2OZAVh9U79T8ZuTFjgmhOQaSH",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_id = await create_user(
                conn,
                telegram_id=930005,
                role="seller",
                username="seller930005",
            )
            await create_account(
                conn,
                owner_user_id=seller_id,
                account_code=f"user:{seller_id}:seller_available",
                account_kind="seller_available",
                balance=Decimal("0.000000"),
            )
            await create_account(
                conn,
                owner_user_id=None,
                account_code="system:system_payout",
                account_kind="system_payout",
                balance=Decimal("1000.000000"),
            )

    await deposit_service.create_seller_deposit_intent(
        seller_user_id=seller_id,
        request_amount_usdt=Decimal("1.23"),
        shard_id=shard.shard_id,
        idempotency_key="intent:page-cap",
    )

    now = datetime.now(UTC)
    page1_op = _op(
        tx_hash="tx-page-cap-1",
        lt=500,
        query_id="q-cap-1",
        trace_id="t-cap-1",
        amount_raw="1300100",
        utime=now,
        src="0:source",
        dst="0:shard",
    )
    page2_op = _op(
        tx_hash="tx-page-cap-2",
        lt=400,
        query_id="q-cap-2",
        trace_id="t-cap-2",
        amount_raw="1300200",
        utime=now - timedelta(seconds=1),
        src="0:source",
        dst="0:shard",
    )
    paged_client = StubTonapiPagedClient(
        {
            None: TonapiJettonHistoryPage(operations=[page1_op], next_from=450),
            450: TonapiJettonHistoryPage(operations=[page2_op], next_from=None),
        },
        shard_raw="0:shard",
    )

    checker = BlockchainCheckerService(
        db_pool,
        advisory_lock_conninfo=isolated_database,
        advisory_lock_id=7008003,
        shard_key="mvp-1",
        shard_address=shard.deposit_address,
        shard_chain="ton_mainnet",
        shard_asset="USDT",
        usdt_jetton_master="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        page_limit=100,
        max_pages_per_shard=1,
        match_batch_size=50,
        confirmations_required=1,
        tonapi_client=paged_client,
        deposit_service=deposit_service,
    )

    first = await checker.run_once()
    cursor_after_first = await deposit_service.get_scan_cursor(
        source_key=f"tonapi:{shard.shard_id}:EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    )
    second = await checker.run_once()
    cursor_after_second = await deposit_service.get_scan_cursor(
        source_key=f"tonapi:{shard.shard_id}:EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    )

    assert first.cursor_updated_count == 1
    assert cursor_after_first.last_lt == 500
    assert cursor_after_first.resume_before_lt == 450
    assert second.cursor_updated_count == 1
    assert cursor_after_second.last_lt == 500
    assert cursor_after_second.resume_before_lt is None
