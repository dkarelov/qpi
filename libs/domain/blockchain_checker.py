from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.domain.deposit_intents import DepositIntentService
from libs.domain.errors import InvalidStateError, NotFoundError
from libs.domain.ledger import FinanceService
from libs.domain.models import PendingWithdrawalView
from libs.integrations.tonapi import TonapiClient, TonapiJettonOperation
from libs.logging.setup import EventLogger, get_logger

_USDT_EXACT_QUANT = Decimal("0.000001")


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
    withdrawals_completed_count: int
    withdrawals_ambiguous_count: int


@dataclass(frozen=True)
class _IngestResult:
    ingested_count: int
    duplicate_count: int
    skipped_not_incoming_count: int
    cursor_updated: bool


@dataclass(frozen=True)
class _WithdrawalScanResult:
    completed_count: int
    ambiguous_count: int
    pending_count: int
    outgoing_tx_count: int


@dataclass(frozen=True)
class _PendingWithdrawalCandidate:
    request_id: int
    amount_usdt: Decimal
    destination_raw: str
    requested_at: datetime


class BlockchainCheckerService:
    """5-minute chain scanner, expected-deposit matcher, and payout verifier."""

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
        finance_service: FinanceService | None = None,
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
        self._finance_service = finance_service or FinanceService(pool)
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
                withdrawals_completed_count=0,
                withdrawals_ambiguous_count=0,
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
            withdrawal_scan = await self._process_withdrawal_payouts()

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
                withdrawals_completed_count=withdrawal_scan.completed_count,
                withdrawals_ambiguous_count=withdrawal_scan.ambiguous_count,
            )
        finally:
            await self._release_lock()

    async def _ingest_shard(self, *, shard_id: int, shard_address: str) -> _IngestResult:
        source_key = f"tonapi:{shard_id}:{self._usdt_jetton_master}"
        cursor = await self._deposit_service.get_scan_cursor(source_key=source_key)
        last_lt = cursor.last_lt
        shard_raw = (await self._tonapi_client.parse_address(account_id=shard_address)).raw_form

        ingested_count = 0
        duplicate_count = 0
        skipped_not_incoming_count = 0
        max_lt_seen = last_lt
        before_lt = cursor.resume_before_lt
        scan_complete = False

        for _ in range(self._max_pages_per_shard):
            page = await self._tonapi_client.get_jetton_account_history(
                account_id=shard_address,
                jetton_id=self._usdt_jetton_master,
                limit=self._page_limit,
                before_lt=before_lt,
            )
            if not page.operations:
                scan_complete = True
                break

            reached_old_cursor = False
            for op in page.operations:
                if before_lt is None and op.lt <= last_lt:
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
                scan_complete = True
                before_lt = None
                break
            if page.next_from is None:
                scan_complete = True
                before_lt = None
                break
            before_lt = page.next_from

        next_last_lt = max(max_lt_seen, cursor.last_lt)
        next_resume_before_lt = None if scan_complete else before_lt
        if (
            next_last_lt != cursor.last_lt
            or next_resume_before_lt != cursor.resume_before_lt
        ):
            await self._deposit_service.set_scan_cursor(
                source_key=source_key,
                last_lt=next_last_lt,
                resume_before_lt=next_resume_before_lt,
            )
            cursor_updated = True
        else:
            cursor_updated = False
        if not scan_complete:
            self._logger.warning(
                "blockchain_checker_ingest_incomplete_page_cap",
                shard_id=shard_id,
                max_pages_per_shard=self._max_pages_per_shard,
                last_lt=last_lt,
                max_lt_seen=max_lt_seen,
                resume_before_lt=before_lt,
            )

        self._logger.info(
            "blockchain_checker_ingest_shard",
            shard_id=shard_id,
            last_lt=last_lt,
            max_lt_seen=max_lt_seen,
            resume_before_lt=cursor.resume_before_lt,
            next_resume_before_lt=next_resume_before_lt,
            ingested_count=ingested_count,
            duplicate_count=duplicate_count,
            skipped_not_incoming_count=skipped_not_incoming_count,
            cursor_updated=cursor_updated,
            scan_complete=scan_complete,
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
                occurred_at=tx.occurred_at,
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

    async def _process_withdrawal_payouts(self) -> _WithdrawalScanResult:
        pending = await self._finance_service.list_pending_withdrawals(limit=self._match_batch_size)
        if not pending:
            self._logger.info(
                "blockchain_checker_withdrawal_match_phase",
                pending_count=0,
                outgoing_tx_count=0,
                completed_count=0,
                ambiguous_count=0,
            )
            return _WithdrawalScanResult(
                completed_count=0,
                ambiguous_count=0,
                pending_count=0,
                outgoing_tx_count=0,
            )

        payout_wallet_raw = _normalize_address(
            (await self._tonapi_client.parse_address(account_id=self._shard_address)).raw_form
        )
        candidates = await self._build_pending_withdrawal_candidates(pending)
        if not candidates:
            self._logger.info(
                "blockchain_checker_withdrawal_match_phase",
                pending_count=len(pending),
                outgoing_tx_count=0,
                completed_count=0,
                ambiguous_count=0,
            )
            return _WithdrawalScanResult(
                completed_count=0,
                ambiguous_count=0,
                pending_count=len(pending),
                outgoing_tx_count=0,
            )

        oldest_requested_at = min(candidate.requested_at for candidate in candidates)
        outgoing_ops = await self._list_recent_payout_operations(
            payout_wallet_raw=payout_wallet_raw,
            oldest_requested_at=oldest_requested_at,
        )
        known_tx_hashes = await self._list_existing_payout_tx_hashes(
            tx_hashes={operation.transaction_hash for operation in outgoing_ops}
        )
        eligible_ops = [
            operation
            for operation in outgoing_ops
            if operation.transaction_hash not in known_tx_hashes
        ]

        matches_by_request: dict[int, list[TonapiJettonOperation]] = defaultdict(list)
        matches_by_tx_hash: dict[str, list[_PendingWithdrawalCandidate]] = defaultdict(list)
        candidates_by_request_id = {candidate.request_id: candidate for candidate in candidates}

        for operation in eligible_ops:
            destination = _normalize_address(operation.destination_address)
            amount = _normalize_usdt(operation.amount_usdt)
            for candidate in candidates:
                if operation.utime < candidate.requested_at:
                    continue
                if amount != candidate.amount_usdt:
                    continue
                if destination != candidate.destination_raw:
                    continue
                matches_by_request[candidate.request_id].append(operation)
                matches_by_tx_hash[operation.transaction_hash].append(candidate)

        ambiguous_request_ids: set[int] = set()
        ambiguous_tx_hashes: set[str] = set()
        completion_pairs: list[tuple[_PendingWithdrawalCandidate, TonapiJettonOperation]] = []

        for request_id, operations in matches_by_request.items():
            if len(operations) != 1:
                ambiguous_request_ids.add(request_id)
                continue
            operation = operations[0]
            tx_candidates = matches_by_tx_hash[operation.transaction_hash]
            if len(tx_candidates) != 1:
                ambiguous_tx_hashes.add(operation.transaction_hash)
                ambiguous_request_ids.update(candidate.request_id for candidate in tx_candidates)
                continue
            completion_pairs.append((candidates_by_request_id[request_id], operation))

        completed_count = 0
        system_payout_account_id = 0
        for candidate, operation in completion_pairs:
            if system_payout_account_id == 0:
                system_payout_account_id = await self._finance_service.ensure_system_account_id(
                    account_kind="system_payout"
                )
            try:
                result = await self._finance_service.complete_withdrawal_request(
                    request_id=candidate.request_id,
                    admin_user_id=None,
                    system_payout_account_id=system_payout_account_id,
                    tx_hash=operation.transaction_hash,
                    idempotency_key=(
                        f"blockchain-checker-withdrawal:{candidate.request_id}:"
                        f"{operation.transaction_hash}"
                    ),
                    completion_source="blockchain_checker",
                )
            except (InvalidStateError, NotFoundError, UniqueViolation) as exc:
                self._logger.warning(
                    "blockchain_checker_withdrawal_completion_skipped",
                    withdrawal_request_id=candidate.request_id,
                    tx_hash=operation.transaction_hash,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                continue
            if result.changed:
                completed_count += 1

        ambiguous_count = len(ambiguous_request_ids)
        self._logger.info(
            "blockchain_checker_withdrawal_match_phase",
            pending_count=len(pending),
            candidate_count=len(candidates),
            outgoing_tx_count=len(outgoing_ops),
            eligible_tx_count=len(eligible_ops),
            completed_count=completed_count,
            ambiguous_count=ambiguous_count,
        )
        return _WithdrawalScanResult(
            completed_count=completed_count,
            ambiguous_count=ambiguous_count,
            pending_count=len(pending),
            outgoing_tx_count=len(outgoing_ops),
        )

    async def _build_pending_withdrawal_candidates(
        self,
        pending: list[PendingWithdrawalView],
    ) -> list[_PendingWithdrawalCandidate]:
        raw_by_address: dict[str, str] = {}
        candidates: list[_PendingWithdrawalCandidate] = []
        for request in pending:
            payout_address = request.payout_address.strip()
            try:
                destination_raw = raw_by_address.get(payout_address)
                if destination_raw is None:
                    destination_raw = _normalize_address(
                        (await self._tonapi_client.parse_address(account_id=payout_address)).raw_form
                    )
                    raw_by_address[payout_address] = destination_raw
            except Exception as exc:
                self._logger.warning(
                    "blockchain_checker_withdrawal_address_parse_failed",
                    withdrawal_request_id=request.withdrawal_request_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                continue
            candidates.append(
                _PendingWithdrawalCandidate(
                    request_id=request.withdrawal_request_id,
                    amount_usdt=_normalize_usdt(request.amount_usdt),
                    destination_raw=destination_raw,
                    requested_at=request.requested_at,
                )
            )
        return candidates

    async def _list_recent_payout_operations(
        self,
        *,
        payout_wallet_raw: str,
        oldest_requested_at: datetime,
    ) -> list[TonapiJettonOperation]:
        operations: list[TonapiJettonOperation] = []
        before_lt: int | None = None

        for _ in range(self._max_pages_per_shard):
            page = await self._tonapi_client.get_jetton_account_history(
                account_id=self._shard_address,
                jetton_id=self._usdt_jetton_master,
                limit=self._page_limit,
                before_lt=before_lt,
            )
            if not page.operations:
                break

            for operation in page.operations:
                if operation.operation != "transfer":
                    continue
                if operation.utime < oldest_requested_at:
                    continue
                if not _is_outgoing_from_payout(
                    operation=operation,
                    payout_wallet_raw=payout_wallet_raw,
                ):
                    continue
                operations.append(operation)

            if page.next_from is None:
                break
            before_lt = page.next_from

        return operations

    async def _list_existing_payout_tx_hashes(self, *, tx_hashes: set[str]) -> set[str]:
        if not tx_hashes:
            return set()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT tx_hash
                    FROM payouts
                    WHERE tx_hash = ANY(%s)
                    """,
                    (list(tx_hashes),),
                )
                rows = await cur.fetchall()
        return {row["tx_hash"] for row in rows}

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


def _is_outgoing_from_payout(*, operation: TonapiJettonOperation, payout_wallet_raw: str) -> bool:
    source = _normalize_address(operation.source_address)
    if not source:
        return False
    return source == payout_wallet_raw


def _normalize_address(address: str | None) -> str:
    return (address or "").strip().lower()


def _normalize_usdt(amount_usdt: Decimal) -> Decimal:
    return amount_usdt.quantize(_USDT_EXACT_QUANT, rounding=ROUND_HALF_UP)
