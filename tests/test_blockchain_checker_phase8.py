from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.blockchain_checker import BlockchainCheckerService
from libs.domain.deposit_intents import DepositIntentService
from libs.domain.errors import InvalidStateError
from libs.integrations.tonapi import (
    TonapiAddressInfo,
    TonapiJettonHistoryPage,
    TonapiJettonOperation,
)
from tests.helpers import create_account, create_user


class StubTonapiClient:
    def __init__(self, operations: list[TonapiJettonOperation], *, shard_raw: str):
        self._operations = operations
        self._shard_raw = shard_raw

    async def parse_address(self, *, account_id: str) -> TonapiAddressInfo:
        return TonapiAddressInfo(raw_form=self._shard_raw)

    async def get_jetton_account_history(
        self,
        *,
        account_id: str,
        jetton_id: str,
        limit: int,
        before_lt: int | None = None,
    ) -> TonapiJettonHistoryPage:
        return TonapiJettonHistoryPage(operations=list(self._operations), next_from=None)


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
