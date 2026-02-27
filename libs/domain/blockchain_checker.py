from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.domain.deposit_intents import DepositIntentService
from libs.integrations.tonapi import TonapiClient, TonapiJettonOperation
from libs.logging.setup import EventLogger, get_logger


@dataclass(frozen=True)
class BlockchainCheckerRunResult:
    lock_acquired: bool
    expired_intents_count: int
    shards_scanned_count: int
    tx_ingested_count: int
    tx_duplicate_count: int
    tx_credited_count: int
    tx_manual_review_count: int
    tx_skipped_not_incoming_count: int
    cursor_updated_count: int


@dataclass(frozen=True)
class _IngestResult:
    ingested_count: int
    duplicate_count: int
    skipped_not_incoming_count: int
    cursor_updated: bool


class BlockchainCheckerService:
    """5-minute chain scanner and expected-deposit matcher."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        advisory_lock_conninfo: str,
        advisory_lock_id: int,
        shard_key: str,
        shard_address: str,
        shard_chain: str,
        shard_asset: str,
        usdt_jetton_master: str,
        page_limit: int,
        max_pages_per_shard: int,
        match_batch_size: int,
        confirmations_required: int,
        tonapi_client: TonapiClient,
        deposit_service: DepositIntentService | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        if advisory_lock_id < 1:
            raise ValueError("advisory_lock_id must be >= 1")
        if page_limit < 1:
            raise ValueError("page_limit must be >= 1")
        if max_pages_per_shard < 1:
            raise ValueError("max_pages_per_shard must be >= 1")
        if match_batch_size < 1:
            raise ValueError("match_batch_size must be >= 1")
        if confirmations_required < 1:
            raise ValueError("confirmations_required must be >= 1")

        self._pool = pool
        self._advisory_lock_conninfo = advisory_lock_conninfo
        self._advisory_lock_id = advisory_lock_id
        self._shard_key = shard_key
        self._shard_address = shard_address
        self._shard_chain = shard_chain
        self._shard_asset = shard_asset
        self._usdt_jetton_master = usdt_jetton_master
        self._page_limit = page_limit
        self._max_pages_per_shard = max_pages_per_shard
        self._match_batch_size = match_batch_size
        self._confirmations_required = confirmations_required
        self._tonapi_client = tonapi_client
        self._deposit_service = deposit_service or DepositIntentService(pool)
        self._logger = logger or get_logger(__name__)
        self._lock_connection: AsyncConnection | None = None

    async def run_once(self) -> BlockchainCheckerRunResult:
        lock_acquired = await self._try_acquire_lock()
        if not lock_acquired:
            self._logger.warning(
                "blockchain_checker_lock_not_acquired",
                advisory_lock_id=self._advisory_lock_id,
            )
            return BlockchainCheckerRunResult(
                lock_acquired=False,
                expired_intents_count=0,
                shards_scanned_count=0,
                tx_ingested_count=0,
                tx_duplicate_count=0,
                tx_credited_count=0,
                tx_manual_review_count=0,
                tx_skipped_not_incoming_count=0,
                cursor_updated_count=0,
            )

        try:
            await self._deposit_service.ensure_default_shard(
                shard_key=self._shard_key,
                deposit_address=self._shard_address,
                chain=self._shard_chain,
                asset=self._shard_asset,
            )

            expired_intents_count = await self._deposit_service.expire_pending_intents()
            shards = await self._deposit_service.list_active_shards()

            tx_ingested_count = 0
            tx_duplicate_count = 0
            tx_skipped_not_incoming_count = 0
            cursor_updated_count = 0

            for shard in shards:
                ingest = await self._ingest_shard(
                    shard_id=shard.shard_id,
                    shard_address=shard.deposit_address,
                )
                tx_ingested_count += ingest.ingested_count
                tx_duplicate_count += ingest.duplicate_count
                tx_skipped_not_incoming_count += ingest.skipped_not_incoming_count
                if ingest.cursor_updated:
                    cursor_updated_count += 1

            tx_credited_count, tx_manual_review_count = await self._process_ingested_txs()

            return BlockchainCheckerRunResult(
                lock_acquired=True,
                expired_intents_count=expired_intents_count,
                shards_scanned_count=len(shards),
                tx_ingested_count=tx_ingested_count,
                tx_duplicate_count=tx_duplicate_count,
                tx_credited_count=tx_credited_count,
                tx_manual_review_count=tx_manual_review_count,
                tx_skipped_not_incoming_count=tx_skipped_not_incoming_count,
                cursor_updated_count=cursor_updated_count,
            )
        finally:
            await self._release_lock()

    async def _ingest_shard(self, *, shard_id: int, shard_address: str) -> _IngestResult:
        source_key = f"tonapi:{shard_id}:{self._usdt_jetton_master}"
        last_lt = await self._deposit_service.get_scan_cursor(source_key=source_key)
        shard_raw = (await self._tonapi_client.parse_address(account_id=shard_address)).raw_form

        ingested_count = 0
        duplicate_count = 0
        skipped_not_incoming_count = 0
        max_lt_seen = last_lt
        before_lt: int | None = None

        for _ in range(self._max_pages_per_shard):
            page = await self._tonapi_client.get_jetton_account_history(
                account_id=shard_address,
                jetton_id=self._usdt_jetton_master,
                limit=self._page_limit,
                before_lt=before_lt,
            )
            if not page.operations:
                break

            reached_old_cursor = False
            for op in page.operations:
                if op.lt <= last_lt:
                    reached_old_cursor = True
                    break
                if op.operation != "transfer":
                    continue
                if op.lt > max_lt_seen:
                    max_lt_seen = op.lt
                if not _is_incoming_to_shard(operation=op, shard_raw=shard_raw):
                    skipped_not_incoming_count += 1
                    continue
                upsert_result = await self._deposit_service.upsert_chain_incoming_tx(
                    shard_id=shard_id,
                    provider="tonapi",
                    chain=self._shard_chain,
                    asset=self._shard_asset,
                    tx_hash=op.transaction_hash,
                    tx_lt=op.lt,
                    query_id=op.query_id,
                    trace_id=op.trace_id,
                    operation_type=op.operation,
                    source_address=op.source_address,
                    destination_address=op.destination_address,
                    amount_raw=op.amount_raw,
                    amount_usdt=op.amount_usdt,
                    occurred_at=op.utime,
                    raw_payload_json=op.payload,
                )
                if upsert_result.created:
                    ingested_count += 1
                else:
                    duplicate_count += 1

            if reached_old_cursor:
                break
            if page.next_from is None:
                break
            before_lt = page.next_from

        if max_lt_seen > last_lt:
            await self._deposit_service.set_scan_cursor(source_key=source_key, last_lt=max_lt_seen)
            cursor_updated = True
        else:
            cursor_updated = False

        self._logger.info(
            "blockchain_checker_ingest_shard",
            shard_id=shard_id,
            last_lt=last_lt,
            max_lt_seen=max_lt_seen,
            ingested_count=ingested_count,
            duplicate_count=duplicate_count,
            skipped_not_incoming_count=skipped_not_incoming_count,
            cursor_updated=cursor_updated,
        )
        return _IngestResult(
            ingested_count=ingested_count,
            duplicate_count=duplicate_count,
            skipped_not_incoming_count=skipped_not_incoming_count,
            cursor_updated=cursor_updated,
        )

    async def _process_ingested_txs(self) -> tuple[int, int]:
        if self._confirmations_required != 1:
            self._logger.warning(
                "blockchain_checker_confirmations_not_supported",
                confirmations_required=self._confirmations_required,
            )

        txs = await self._deposit_service.list_chain_txs_for_matching(limit=self._match_batch_size)
        credited_count = 0
        manual_review_count = 0

        for tx in txs:
            if tx.suffix_code is None:
                await self._deposit_service.mark_chain_tx_manual_review(
                    chain_tx_id=tx.chain_tx_id,
                    reason="invalid_suffix",
                    matched_intent_id=None,
                    promote_intent_to_manual_review=False,
                )
                manual_review_count += 1
                continue

            intent = await self._deposit_service.get_active_intent_by_suffix(
                shard_id=tx.shard_id,
                suffix_code=tx.suffix_code,
            )
            if intent is None:
                fallback = await self._deposit_service.get_latest_intent_by_suffix(
                    shard_id=tx.shard_id,
                    suffix_code=tx.suffix_code,
                )
                await self._deposit_service.mark_chain_tx_manual_review(
                    chain_tx_id=tx.chain_tx_id,
                    reason="no_active_intent_for_suffix",
                    matched_intent_id=fallback.deposit_intent_id if fallback else None,
                    promote_intent_to_manual_review=False,
                )
                manual_review_count += 1
                continue

            if intent.status != "pending":
                await self._deposit_service.mark_chain_tx_manual_review(
                    chain_tx_id=tx.chain_tx_id,
                    reason="intent_not_pending",
                    matched_intent_id=intent.deposit_intent_id,
                    promote_intent_to_manual_review=False,
                )
                manual_review_count += 1
                continue

            if tx.occurred_at > intent.expires_at:
                await self._deposit_service.mark_chain_tx_manual_review(
                    chain_tx_id=tx.chain_tx_id,
                    reason="late_payment",
                    matched_intent_id=intent.deposit_intent_id,
                    promote_intent_to_manual_review=True,
                )
                manual_review_count += 1
                continue

            if tx.amount_usdt < intent.expected_amount_usdt:
                await self._deposit_service.mark_chain_tx_manual_review(
                    chain_tx_id=tx.chain_tx_id,
                    reason="partial_payment",
                    matched_intent_id=intent.deposit_intent_id,
                    promote_intent_to_manual_review=True,
                )
                manual_review_count += 1
                continue

            credit = await self._deposit_service.credit_intent_from_chain_tx(
                deposit_intent_id=intent.deposit_intent_id,
                chain_tx_id=tx.chain_tx_id,
                idempotency_key=(
                    f"blockchain-checker-credit:{intent.deposit_intent_id}:{tx.chain_tx_id}"
                ),
            )
            if credit.changed:
                credited_count += 1

        self._logger.info(
            "blockchain_checker_match_phase",
            scanned_count=len(txs),
            credited_count=credited_count,
            manual_review_count=manual_review_count,
        )
        return credited_count, manual_review_count

    async def _try_acquire_lock(self) -> bool:
        self._lock_connection = await AsyncConnection.connect(
            self._advisory_lock_conninfo,
            autocommit=True,
            row_factory=dict_row,
        )
        async with self._lock_connection.cursor() as cur:
            await cur.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (self._advisory_lock_id,),
            )
            row = await cur.fetchone()
            acquired = bool(row["acquired"])
        if not acquired:
            await self._lock_connection.close()
            self._lock_connection = None
        return acquired

    async def _release_lock(self) -> None:
        if self._lock_connection is None:
            return
        try:
            async with self._lock_connection.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (self._advisory_lock_id,))
        finally:
            await self._lock_connection.close()
            self._lock_connection = None


def _is_incoming_to_shard(*, operation: TonapiJettonOperation, shard_raw: str) -> bool:
    destination = (operation.destination_address or "").strip().lower()
    if not destination:
        return False
    return destination == shard_raw.strip().lower()
