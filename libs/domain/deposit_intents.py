from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.errors import InvalidStateError, NotFoundError
from libs.domain.ledger import FinanceService
from libs.domain.models import (
    AdminDepositReviewTxView,
    AdminExpiredDepositIntentView,
    ChainIncomingTxRow,
    ChainTxUpsertResult,
    DepositIntentCreateResult,
    DepositIntentCreditResult,
    DepositIntentRow,
    DepositShardView,
    SellerDepositIntentView,
)
from libs.domain.notifications import NotificationService
from libs.logging.setup import EventLogger, get_logger

_ACTIVE_SUFFIX_STATUSES = ("pending", "matched", "manual_review")


@dataclass(frozen=True)
class _ScanCursor:
    last_lt: int
    resume_before_lt: int | None


class DepositIntentService:
    """Expected-deposit lifecycle for seller collateral top-ups."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        invoice_ttl_hours: int = 24,
        finance_service: FinanceService | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        if invoice_ttl_hours < 1:
            raise ValueError("invoice_ttl_hours must be >= 1")
        self._pool = pool
        self._invoice_ttl_hours = invoice_ttl_hours
        self._finance = finance_service or FinanceService(pool)
        self._notifications = NotificationService(pool)
        self._logger = logger or get_logger(__name__)

    async def ensure_default_shard(
        self,
        *,
        shard_key: str,
        deposit_address: str,
        chain: str = "ton_mainnet",
        asset: str = "USDT",
    ) -> DepositShardView:
        normalized_key = shard_key.strip()
        normalized_address = deposit_address.strip()
        if not normalized_key:
            raise ValueError("shard_key must not be empty")
        if not normalized_address:
            raise ValueError("deposit_address must not be empty")

        async def operation(conn: AsyncConnection) -> DepositShardView:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO deposit_shards (
                        shard_key,
                        deposit_address,
                        chain,
                        asset,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (shard_key)
                    DO UPDATE SET
                        deposit_address = EXCLUDED.deposit_address,
                        chain = EXCLUDED.chain,
                        asset = EXCLUDED.asset,
                        is_active = TRUE,
                        updated_at = timezone('utc', now())
                    RETURNING id, shard_key, deposit_address, chain, asset, is_active
                    """,
                    (normalized_key, normalized_address, chain, asset),
                )
                row = await cur.fetchone()
                return DepositShardView(
                    shard_id=row["id"],
                    shard_key=row["shard_key"],
                    deposit_address=row["deposit_address"],
                    chain=row["chain"],
                    asset=row["asset"],
                    is_active=row["is_active"],
                )

        return await run_in_transaction(self._pool, operation)

    async def list_active_shards(self) -> list[DepositShardView]:
        async def operation(conn: AsyncConnection) -> list[DepositShardView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, shard_key, deposit_address, chain, asset, is_active
                    FROM deposit_shards
                    WHERE is_active = TRUE
                    ORDER BY id ASC
                    """
                )
                rows = await cur.fetchall()
                return [
                    DepositShardView(
                        shard_id=row["id"],
                        shard_key=row["shard_key"],
                        deposit_address=row["deposit_address"],
                        chain=row["chain"],
                        asset=row["asset"],
                        is_active=row["is_active"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def create_seller_deposit_intent(
        self,
        *,
        seller_user_id: int,
        request_amount_usdt: Decimal,
        shard_id: int,
        idempotency_key: str,
    ) -> DepositIntentCreateResult:
        normalized_amount = _normalize_amount(request_amount_usdt)
        if normalized_amount <= Decimal("0.000000"):
            raise ValueError("request_amount_usdt must be > 0")

        async def operation(conn: AsyncConnection) -> DepositIntentCreateResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_seller_user(cur, seller_user_id=seller_user_id)
                target_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=seller_user_id,
                    account_kind="seller_available",
                )

                await cur.execute(
                    """
                    SELECT
                        di.id,
                        di.shard_id,
                        ds.deposit_address,
                        di.request_amount_usdt,
                        di.base_amount_usdt,
                        di.expected_amount_usdt,
                        di.suffix_code,
                        di.expires_at
                    FROM deposit_intents di
                    JOIN deposit_shards ds ON ds.id = di.shard_id
                    WHERE di.idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    return DepositIntentCreateResult(
                        deposit_intent_id=existing["id"],
                        shard_id=existing["shard_id"],
                        deposit_address=existing["deposit_address"],
                        request_amount_usdt=_normalize_amount(existing["request_amount_usdt"]),
                        base_amount_usdt=_normalize_amount(existing["base_amount_usdt"]),
                        expected_amount_usdt=_normalize_amount(existing["expected_amount_usdt"]),
                        suffix_code=int(existing["suffix_code"]),
                        expires_at=existing["expires_at"],
                        created=False,
                    )

                await cur.execute(
                    """
                    SELECT id, deposit_address, is_active
                    FROM deposit_shards
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (shard_id,),
                )
                shard = await cur.fetchone()
                if shard is None:
                    raise NotFoundError(f"deposit shard {shard_id} not found")
                if not shard["is_active"]:
                    raise InvalidStateError("deposit shard is inactive")

                await self._expire_pending_intents_locked(cur)

                await cur.execute(
                    """
                    SELECT suffix_code
                    FROM deposit_intents
                    WHERE shard_id = %s
                      AND status = ANY(%s)
                    """,
                    (shard_id, list(_ACTIVE_SUFFIX_STATUSES)),
                )
                busy_suffixes = {int(row["suffix_code"]) for row in await cur.fetchall()}
                remaining_suffixes = 999 - len(busy_suffixes)
                if remaining_suffixes <= 50:
                    self._logger.warning(
                        "deposit_intent_suffix_pressure",
                        shard_id=shard_id,
                        seller_user_id=seller_user_id,
                        active_suffixes_count=len(busy_suffixes),
                        remaining_suffixes=remaining_suffixes,
                    )
                suffix_code = _allocate_suffix(busy_suffixes)
                if suffix_code is None:
                    raise InvalidStateError("all 999 suffixes are currently occupied")

                base_amount = _round_up_to_tenth(normalized_amount)
                expected_amount = _normalize_amount(
                    base_amount + (Decimal(suffix_code) / Decimal("10000"))
                )

                await cur.execute(
                    """
                    INSERT INTO deposit_intents (
                        seller_user_id,
                        target_account_id,
                        shard_id,
                        request_amount_usdt,
                        base_amount_usdt,
                        expected_amount_usdt,
                        suffix_code,
                        status,
                        expires_at,
                        idempotency_key
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        'pending',
                        timezone('utc', now()) + (%s * interval '1 hour'),
                        %s
                    )
                    RETURNING id, expires_at
                    """,
                    (
                        seller_user_id,
                        target_account_id,
                        shard_id,
                        normalized_amount,
                        base_amount,
                        expected_amount,
                        suffix_code,
                        self._invoice_ttl_hours,
                        idempotency_key,
                    ),
                )
                created = await cur.fetchone()
                return DepositIntentCreateResult(
                    deposit_intent_id=created["id"],
                    shard_id=shard_id,
                    deposit_address=shard["deposit_address"],
                    request_amount_usdt=normalized_amount,
                    base_amount_usdt=base_amount,
                    expected_amount_usdt=expected_amount,
                    suffix_code=suffix_code,
                    expires_at=created["expires_at"],
                    created=True,
                )

        return await run_in_transaction(self._pool, operation)

    async def list_seller_deposit_intents(
        self,
        *,
        seller_user_id: int,
        limit: int = 10,
    ) -> list[SellerDepositIntentView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[SellerDepositIntentView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        di.id,
                        di.status,
                        di.request_amount_usdt,
                        di.expected_amount_usdt,
                        di.suffix_code,
                        ds.deposit_address,
                        di.expires_at,
                        di.created_at,
                        di.credited_amount_usdt,
                        di.review_reason,
                        tx.tx_hash
                    FROM deposit_intents di
                    JOIN deposit_shards ds ON ds.id = di.shard_id
                    LEFT JOIN chain_incoming_txs tx ON tx.id = di.matched_chain_tx_id
                    WHERE di.seller_user_id = %s
                    ORDER BY di.created_at DESC, di.id DESC
                    LIMIT %s
                    """,
                    (seller_user_id, limit),
                )
                rows = await cur.fetchall()
                return [
                    SellerDepositIntentView(
                        deposit_intent_id=row["id"],
                        status=row["status"],
                        request_amount_usdt=_normalize_amount(row["request_amount_usdt"]),
                        expected_amount_usdt=_normalize_amount(row["expected_amount_usdt"]),
                        suffix_code=int(row["suffix_code"]),
                        deposit_address=row["deposit_address"],
                        expires_at=row["expires_at"],
                        created_at=row["created_at"],
                        credited_amount_usdt=(
                            _normalize_amount(row["credited_amount_usdt"])
                            if row["credited_amount_usdt"] is not None
                            else None
                        ),
                        tx_hash=row["tx_hash"],
                        review_reason=row["review_reason"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def expire_pending_intents(self) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._expire_pending_intents_locked(cur)

        return await run_in_transaction(self._pool, operation)

    async def get_scan_cursor(self, *, source_key: str) -> _ScanCursor:
        async def operation(conn: AsyncConnection) -> _ScanCursor:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT last_lt, resume_before_lt
                    FROM chain_scan_cursors
                    WHERE source_key = %s
                    """,
                    (source_key,),
                )
                row = await cur.fetchone()
                if row is None:
                    return _ScanCursor(last_lt=0, resume_before_lt=None)
                return _ScanCursor(
                    last_lt=int(row["last_lt"]),
                    resume_before_lt=(
                        int(row["resume_before_lt"])
                        if row["resume_before_lt"] is not None
                        else None
                    ),
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def set_scan_cursor(
        self,
        *,
        source_key: str,
        last_lt: int,
        resume_before_lt: int | None = None,
    ) -> None:
        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO chain_scan_cursors (source_key, last_lt, resume_before_lt)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (source_key)
                    DO UPDATE SET
                        last_lt = EXCLUDED.last_lt,
                        resume_before_lt = EXCLUDED.resume_before_lt,
                        updated_at = timezone('utc', now())
                    """,
                    (source_key, last_lt, resume_before_lt),
                )

        await run_in_transaction(self._pool, operation)

    async def upsert_chain_incoming_tx(
        self,
        *,
        shard_id: int,
        provider: str,
        chain: str,
        asset: str,
        tx_hash: str,
        tx_lt: int,
        query_id: str,
        trace_id: str,
        operation_type: str,
        source_address: str | None,
        destination_address: str | None,
        amount_raw: str,
        amount_usdt: Decimal,
        occurred_at,
        raw_payload_json: dict,
    ) -> ChainTxUpsertResult:
        normalized_amount = _normalize_amount(amount_usdt)
        suffix_code = derive_suffix_code(normalized_amount)

        async def operation(conn: AsyncConnection) -> ChainTxUpsertResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO chain_incoming_txs (
                        shard_id,
                        provider,
                        chain,
                        asset,
                        tx_hash,
                        tx_lt,
                        query_id,
                        trace_id,
                        operation_type,
                        source_address,
                        destination_address,
                        amount_raw,
                        amount_usdt,
                        occurred_at,
                        suffix_code,
                        status,
                        raw_payload_json
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        'ingested',
                        %s
                    )
                    ON CONFLICT (shard_id, tx_hash, tx_lt, query_id, operation_type)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        shard_id,
                        provider,
                        chain,
                        asset,
                        tx_hash,
                        tx_lt,
                        query_id,
                        trace_id,
                        operation_type,
                        source_address,
                        destination_address,
                        amount_raw,
                        normalized_amount,
                        occurred_at,
                        suffix_code,
                        Json(raw_payload_json),
                    ),
                )
                row = await cur.fetchone()
                if row is not None:
                    return ChainTxUpsertResult(chain_tx_id=row["id"], created=True)

                await cur.execute(
                    """
                    SELECT id
                    FROM chain_incoming_txs
                    WHERE shard_id = %s
                      AND tx_hash = %s
                      AND tx_lt = %s
                      AND query_id = %s
                      AND operation_type = %s
                    """,
                    (shard_id, tx_hash, tx_lt, query_id, operation_type),
                )
                existing = await cur.fetchone()
                return ChainTxUpsertResult(chain_tx_id=existing["id"], created=False)

        return await run_in_transaction(self._pool, operation)

    async def list_chain_txs_for_matching(self, *, limit: int = 200) -> list[ChainIncomingTxRow]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[ChainIncomingTxRow]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        shard_id,
                        tx_hash,
                        tx_lt,
                        query_id,
                        trace_id,
                        source_address,
                        destination_address,
                        amount_raw,
                        amount_usdt,
                        occurred_at,
                        suffix_code,
                        status,
                        matched_intent_id,
                        review_reason
                    FROM chain_incoming_txs
                    WHERE status = 'ingested'
                    ORDER BY occurred_at ASC, id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
                return [
                    ChainIncomingTxRow(
                        chain_tx_id=row["id"],
                        shard_id=row["shard_id"],
                        tx_hash=row["tx_hash"],
                        tx_lt=int(row["tx_lt"]),
                        query_id=row["query_id"],
                        trace_id=row["trace_id"],
                        source_address=row["source_address"],
                        destination_address=row["destination_address"],
                        amount_raw=row["amount_raw"],
                        amount_usdt=_normalize_amount(row["amount_usdt"]),
                        occurred_at=row["occurred_at"],
                        suffix_code=row["suffix_code"],
                        status=row["status"],
                        matched_intent_id=row["matched_intent_id"],
                        review_reason=row["review_reason"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_active_intent_by_suffix(
        self,
        *,
        shard_id: int,
        suffix_code: int,
    ) -> DepositIntentRow | None:
        async def operation(conn: AsyncConnection) -> DepositIntentRow | None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        seller_user_id,
                        target_account_id,
                        shard_id,
                        status,
                        expected_amount_usdt,
                        suffix_code,
                        expires_at
                    FROM deposit_intents
                    WHERE shard_id = %s
                      AND suffix_code = %s
                      AND status IN ('pending', 'matched', 'manual_review')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (shard_id, suffix_code),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return DepositIntentRow(
                    deposit_intent_id=row["id"],
                    seller_user_id=row["seller_user_id"],
                    target_account_id=row["target_account_id"],
                    shard_id=row["shard_id"],
                    status=row["status"],
                    expected_amount_usdt=_normalize_amount(row["expected_amount_usdt"]),
                    suffix_code=int(row["suffix_code"]),
                    expires_at=row["expires_at"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_latest_intent_by_suffix(
        self,
        *,
        shard_id: int,
        suffix_code: int,
    ) -> DepositIntentRow | None:
        async def operation(conn: AsyncConnection) -> DepositIntentRow | None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        seller_user_id,
                        target_account_id,
                        shard_id,
                        status,
                        expected_amount_usdt,
                        suffix_code,
                        expires_at
                    FROM deposit_intents
                    WHERE shard_id = %s
                      AND suffix_code = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (shard_id, suffix_code),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return DepositIntentRow(
                    deposit_intent_id=row["id"],
                    seller_user_id=row["seller_user_id"],
                    target_account_id=row["target_account_id"],
                    shard_id=row["shard_id"],
                    status=row["status"],
                    expected_amount_usdt=_normalize_amount(row["expected_amount_usdt"]),
                    suffix_code=int(row["suffix_code"]),
                    expires_at=row["expires_at"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def mark_chain_tx_manual_review(
        self,
        *,
        chain_tx_id: int,
        reason: str,
        matched_intent_id: int | None,
        promote_intent_to_manual_review: bool,
    ) -> None:
        normalized_reason = reason.strip() or "manual_review"

        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM chain_incoming_txs
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (chain_tx_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"chain incoming tx {chain_tx_id} not found")

                await cur.execute(
                    """
                    UPDATE chain_incoming_txs
                    SET status = 'manual_review',
                        review_reason = %s,
                        matched_intent_id = %s,
                        processed_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (normalized_reason, matched_intent_id, chain_tx_id),
                )

                if promote_intent_to_manual_review and matched_intent_id is not None:
                    await cur.execute(
                        """
                        UPDATE deposit_intents
                        SET status = CASE
                            WHEN status IN ('pending', 'matched') THEN 'manual_review'
                            ELSE status
                        END,
                        review_reason = CASE
                            WHEN status IN ('pending', 'matched') THEN %s
                            ELSE review_reason
                        END,
                        updated_at = timezone('utc', now())
                        WHERE id = %s
                        """,
                        (normalized_reason, matched_intent_id),
                    )

                await self._notifications.enqueue_deposit_manual_review_locked(
                    cur,
                    chain_tx_id=chain_tx_id,
                    matched_intent_id=matched_intent_id,
                    reason=normalized_reason,
                )

        await run_in_transaction(self._pool, operation)

    async def credit_intent_from_chain_tx(
        self,
        *,
        deposit_intent_id: int,
        chain_tx_id: int,
        idempotency_key: str,
        admin_user_id: int | None = None,
        allow_expired: bool = False,
    ) -> DepositIntentCreditResult:
        async def operation(conn: AsyncConnection) -> DepositIntentCreditResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        seller_user_id,
                        target_account_id,
                        status,
                        matched_chain_tx_id,
                        credited_ledger_entry_id
                    FROM deposit_intents
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (deposit_intent_id,),
                )
                intent = await cur.fetchone()
                if intent is None:
                    raise NotFoundError(f"deposit intent {deposit_intent_id} not found")

                await cur.execute(
                    """
                    SELECT
                        id,
                        tx_hash,
                        amount_usdt,
                        status,
                        matched_intent_id,
                        credited_ledger_entry_id
                    FROM chain_incoming_txs
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (chain_tx_id,),
                )
                tx = await cur.fetchone()
                if tx is None:
                    raise NotFoundError(f"chain incoming tx {chain_tx_id} not found")

                if (
                    intent["status"] == "credited"
                    and tx["status"] == "credited"
                    and intent["matched_chain_tx_id"] == chain_tx_id
                    and tx["matched_intent_id"] == deposit_intent_id
                ):
                    return DepositIntentCreditResult(
                        changed=False,
                        ledger_entry_id=intent["credited_ledger_entry_id"],
                        credited_amount_usdt=_normalize_amount(tx["amount_usdt"]),
                    )

                if intent["status"] == "cancelled":
                    raise InvalidStateError("deposit intent is cancelled")
                if tx["status"] == "credited" and tx["matched_intent_id"] != deposit_intent_id:
                    raise InvalidStateError("chain tx already credited to another intent")
                if intent["status"] == "credited" and intent["matched_chain_tx_id"] != chain_tx_id:
                    raise InvalidStateError("deposit intent already credited by another tx")

                if allow_expired:
                    allowed_statuses = {"pending", "manual_review", "matched", "expired"}
                else:
                    allowed_statuses = {"pending", "manual_review", "matched"}
                if intent["status"] not in allowed_statuses:
                    raise InvalidStateError(
                        "deposit intent cannot be credited from current status"
                    )

                amount = _normalize_amount(tx["amount_usdt"])
                system_payout_account_id = await self._finance.ensure_system_account_locked(
                    cur,
                    account_kind="system_payout",
                )
                await self._finance.provision_system_balance_locked(
                    cur,
                    account_id=system_payout_account_id,
                    amount_usdt=amount,
                    event_type="expected_deposit_credit",
                    idempotency_key=f"{idempotency_key}:provision",
                    metadata={
                        "deposit_intent_id": deposit_intent_id,
                        "chain_tx_id": chain_tx_id,
                        "tx_hash": tx["tx_hash"],
                        "seller_user_id": intent["seller_user_id"],
                        "target_account_id": intent["target_account_id"],
                        "amount_usdt": str(amount),
                    },
                )
                transfer = await self._finance.transfer_locked(
                    cur,
                    from_account_id=system_payout_account_id,
                    to_account_id=intent["target_account_id"],
                    amount_usdt=amount,
                    event_type="expected_deposit_credit",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="deposit_intent",
                    entity_id=deposit_intent_id,
                    metadata={
                        "deposit_intent_id": deposit_intent_id,
                        "chain_tx_id": chain_tx_id,
                        "tx_hash": tx["tx_hash"],
                        "seller_user_id": intent["seller_user_id"],
                        "target_account_id": intent["target_account_id"],
                        "amount_usdt": str(amount),
                    },
                )

                await cur.execute(
                    """
                    UPDATE deposit_intents
                    SET status = 'credited',
                        matched_chain_tx_id = %s,
                        credited_ledger_entry_id = %s,
                        credited_amount_usdt = %s,
                        review_reason = NULL,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (chain_tx_id, transfer.entry_id, amount, deposit_intent_id),
                )
                await cur.execute(
                    """
                    UPDATE chain_incoming_txs
                    SET status = 'credited',
                        matched_intent_id = %s,
                        credited_ledger_entry_id = %s,
                        review_reason = NULL,
                        processed_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (deposit_intent_id, transfer.entry_id, chain_tx_id),
                )

                if admin_user_id is not None:
                    await self._finance.insert_admin_audit_locked(
                        cur,
                        admin_user_id=admin_user_id,
                        action="deposit_intent_attach_and_credit",
                        target_type="deposit_intent",
                        target_id=str(deposit_intent_id),
                        payload={
                            "deposit_intent_id": deposit_intent_id,
                            "chain_tx_id": chain_tx_id,
                            "tx_hash": tx["tx_hash"],
                            "ledger_entry_id": transfer.entry_id,
                            "amount_usdt": str(amount),
                        },
                        idempotency_key=f"{idempotency_key}:audit",
                    )

                await self._notifications.enqueue_deposit_credited_locked(
                    cur,
                    deposit_intent_id=deposit_intent_id,
                )

                return DepositIntentCreditResult(
                    changed=True,
                    ledger_entry_id=transfer.entry_id,
                    credited_amount_usdt=amount,
                )

        return await run_in_transaction(self._pool, operation)

    async def cancel_deposit_intent(
        self,
        *,
        deposit_intent_id: int,
        admin_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> bool:
        normalized_reason = reason.strip() or "cancelled_by_admin"

        async def operation(conn: AsyncConnection) -> bool:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status
                    FROM deposit_intents
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (deposit_intent_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"deposit intent {deposit_intent_id} not found")
                if row["status"] == "cancelled":
                    return False
                if row["status"] == "credited":
                    raise InvalidStateError("credited deposit intent cannot be cancelled")

                await cur.execute(
                    """
                    UPDATE deposit_intents
                    SET status = 'cancelled',
                        review_reason = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (normalized_reason, deposit_intent_id),
                )
                await self._finance.insert_admin_audit_locked(
                    cur,
                    admin_user_id=admin_user_id,
                    action="deposit_intent_cancelled",
                    target_type="deposit_intent",
                    target_id=str(deposit_intent_id),
                    payload={
                        "deposit_intent_id": deposit_intent_id,
                        "reason": normalized_reason,
                    },
                    idempotency_key=idempotency_key,
                )
                await self._notifications.enqueue_deposit_cancelled_locked(
                    cur,
                    deposit_intent_id=deposit_intent_id,
                )
                return True

        return await run_in_transaction(self._pool, operation)

    async def list_admin_review_txs(self, *, limit: int = 20) -> list[AdminDepositReviewTxView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[AdminDepositReviewTxView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        tx.id,
                        tx.shard_id,
                        ds.deposit_address,
                        tx.tx_hash,
                        tx.amount_usdt,
                        tx.suffix_code,
                        tx.status,
                        tx.review_reason,
                        tx.occurred_at,
                        tx.matched_intent_id
                    FROM chain_incoming_txs tx
                    JOIN deposit_shards ds ON ds.id = tx.shard_id
                    WHERE tx.status = 'manual_review'
                    ORDER BY tx.occurred_at DESC, tx.id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
                return [
                    AdminDepositReviewTxView(
                        chain_tx_id=row["id"],
                        shard_id=row["shard_id"],
                        deposit_address=row["deposit_address"],
                        tx_hash=row["tx_hash"],
                        amount_usdt=_normalize_amount(row["amount_usdt"]),
                        suffix_code=row["suffix_code"],
                        status=row["status"],
                        review_reason=row["review_reason"],
                        occurred_at=row["occurred_at"],
                        matched_intent_id=row["matched_intent_id"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def list_admin_expired_intents(
        self,
        *,
        limit: int = 20,
    ) -> list[AdminExpiredDepositIntentView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[AdminExpiredDepositIntentView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        di.id,
                        di.seller_user_id,
                        u.telegram_id,
                        di.expected_amount_usdt,
                        di.suffix_code,
                        di.status,
                        di.expires_at
                    FROM deposit_intents di
                    JOIN users u ON u.id = di.seller_user_id
                    WHERE di.status = 'expired'
                    ORDER BY di.expires_at DESC, di.id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
                return [
                    AdminExpiredDepositIntentView(
                        deposit_intent_id=row["id"],
                        seller_user_id=row["seller_user_id"],
                        seller_telegram_id=row["telegram_id"],
                        expected_amount_usdt=_normalize_amount(row["expected_amount_usdt"]),
                        suffix_code=int(row["suffix_code"]),
                        status=row["status"],
                        expires_at=row["expires_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def _expire_pending_intents_locked(self, cur) -> int:
        await cur.execute(
            """
            UPDATE deposit_intents
            SET status = 'expired',
                updated_at = timezone('utc', now())
            WHERE status IN ('pending', 'matched')
              AND expires_at < timezone('utc', now())
            RETURNING id
            """
        )
        rows = await cur.fetchall()
        for row in rows:
            await self._notifications.enqueue_deposit_expired_locked(
                cur,
                deposit_intent_id=row["id"],
            )
        return len(rows)

    async def _ensure_seller_user(self, cur, *, seller_user_id: int) -> None:
        await cur.execute(
            """
            SELECT id
            FROM users
            WHERE id = %s
              AND (
                    is_seller
                    OR is_admin
                    OR role IN ('seller', 'admin')
              )
            """,
            (seller_user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"seller user {seller_user_id} not found")

    async def _ensure_owner_account(self, cur, *, owner_user_id: int, account_kind: str) -> int:
        return await self._finance.ensure_owner_account_locked(
            cur,
            owner_user_id=owner_user_id,
            account_kind=account_kind,
        )


def derive_suffix_code(amount_usdt: Decimal) -> int | None:
    """Extract 3-digit amount suffix from 4th decimal contract."""

    scaled = (amount_usdt * Decimal("10000")).to_integral_value(rounding=ROUND_FLOOR)
    suffix = int(scaled % 1000)
    if suffix < 1 or suffix > 999:
        return None
    return suffix


def _allocate_suffix(busy_suffixes: set[int]) -> int | None:
    for value in range(1, 1000):
        if value not in busy_suffixes:
            return value
    return None


def _round_up_to_tenth(amount: Decimal) -> Decimal:
    scaled = (amount * Decimal("10")).to_integral_value(rounding=ROUND_CEILING)
    return _normalize_amount(scaled / Decimal("10"))


def _ledger_key(idempotency_key: str) -> str:
    return f"ledger:{idempotency_key}"


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
