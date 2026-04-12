from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.buyer import BuyerService
from libs.domain.errors import InvalidStateError, NotFoundError
from libs.domain.ledger import FinanceService
from libs.domain.models import ReservationExpiryResult
from libs.domain.notifications import NotificationService
from libs.logging.setup import EventLogger, get_logger

_PICKUP_OPERATION = "Продажа"
_RETURN_OPERATION = "Возврат"
_RETURN_IDEMPOTENCY_PREFIX = "order-tracker:return"
_DELIVERY_EXPIRED_IDEMPOTENCY_PREFIX = "order-tracker:delivery-expired"
_UNLOCK_IDEMPOTENCY_PREFIX = "order-tracker:unlock"


@dataclass(frozen=True)
class _WbEventCandidate:
    assignment_id: int
    assignment_status: str
    seller_collateral_account_id: int
    reward_reserved_account_id: int
    sale_at: datetime | None
    return_at: datetime | None
    unlock_at: datetime | None


@dataclass(frozen=True)
class _DeliveryExpiredCandidate:
    assignment_id: int
    seller_collateral_account_id: int
    reward_reserved_account_id: int


@dataclass(frozen=True)
class _UnlockCandidate:
    assignment_id: int
    buyer_user_id: int
    reward_reserved_account_id: int


@dataclass(frozen=True)
class WbEventProcessResult:
    processed_count: int
    pickup_count: int
    pickup_skipped_count: int
    return_cancelled_count: int
    return_ignored_after_unlock_count: int
    return_skipped_count: int


@dataclass(frozen=True)
class BatchProcessResult:
    processed_count: int
    changed_count: int
    skipped_count: int


@dataclass(frozen=True)
class OrderTrackerRunResult:
    lock_acquired: bool
    reservation_expiry_processed_count: int
    reservation_expiry_changed_count: int
    wb_processed_count: int
    wb_pickup_count: int
    wb_return_cancelled_count: int
    wb_return_ignored_after_unlock_count: int
    delivery_expired_processed_count: int
    delivery_expired_changed_count: int
    unlock_processed_count: int
    unlock_changed_count: int


class OrderTrackerService:
    """5-minute orchestrator for assignment lifecycle transitions."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        advisory_lock_conninfo: str,
        advisory_lock_id: int,
        reservation_expiry_batch_size: int,
        wb_event_batch_size: int,
        delivery_expiry_batch_size: int,
        unlock_batch_size: int,
        delivery_expiry_days: int,
        unlock_days: int,
        buyer_service: BuyerService | None = None,
        finance_service: FinanceService | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        if advisory_lock_id < 1:
            raise ValueError("advisory_lock_id must be >= 1")
        if reservation_expiry_batch_size < 1:
            raise ValueError("reservation_expiry_batch_size must be >= 1")
        if wb_event_batch_size < 1:
            raise ValueError("wb_event_batch_size must be >= 1")
        if delivery_expiry_batch_size < 1:
            raise ValueError("delivery_expiry_batch_size must be >= 1")
        if unlock_batch_size < 1:
            raise ValueError("unlock_batch_size must be >= 1")
        if delivery_expiry_days < 1:
            raise ValueError("delivery_expiry_days must be >= 1")
        if unlock_days < 1:
            raise ValueError("unlock_days must be >= 1")

        self._pool = pool
        self._advisory_lock_conninfo = advisory_lock_conninfo
        self._advisory_lock_id = advisory_lock_id
        self._reservation_expiry_batch_size = reservation_expiry_batch_size
        self._wb_event_batch_size = wb_event_batch_size
        self._delivery_expiry_batch_size = delivery_expiry_batch_size
        self._unlock_batch_size = unlock_batch_size
        self._delivery_expiry_days = delivery_expiry_days
        self._unlock_days = unlock_days
        self._buyer_service = buyer_service or BuyerService(pool)
        self._finance_service = finance_service or FinanceService(pool)
        self._notifications = NotificationService(pool)
        self._logger = logger or get_logger(__name__)
        self._lock_connection: AsyncConnection | None = None

    async def run_once(self) -> OrderTrackerRunResult:
        lock_acquired = await self._try_acquire_lock()
        if not lock_acquired:
            self._logger.warning(
                "order_tracker_lock_not_acquired",
                advisory_lock_id=self._advisory_lock_id,
            )
            return OrderTrackerRunResult(
                lock_acquired=False,
                reservation_expiry_processed_count=0,
                reservation_expiry_changed_count=0,
                wb_processed_count=0,
                wb_pickup_count=0,
                wb_return_cancelled_count=0,
                wb_return_ignored_after_unlock_count=0,
                delivery_expired_processed_count=0,
                delivery_expired_changed_count=0,
                unlock_processed_count=0,
                unlock_changed_count=0,
            )

        try:
            reservation_result = await self._process_expired_reservations()
            self._logger.info(
                "order_tracker_reservation_expiry_phase",
                processed_count=reservation_result.processed_count,
                changed_count=reservation_result.expired_count,
                skipped_count=reservation_result.processed_count - reservation_result.expired_count,
                batch_size=self._reservation_expiry_batch_size,
            )
            wb_result = await self._process_wb_events()
            self._logger.info(
                "order_tracker_wb_phase",
                processed_count=wb_result.processed_count,
                pickup_count=wb_result.pickup_count,
                pickup_skipped_count=wb_result.pickup_skipped_count,
                return_cancelled_count=wb_result.return_cancelled_count,
                return_ignored_after_unlock_count=wb_result.return_ignored_after_unlock_count,
                return_skipped_count=wb_result.return_skipped_count,
                batch_size=self._wb_event_batch_size,
            )
            delivery_expired_result = await self._process_delivery_expired()
            self._logger.info(
                "order_tracker_delivery_expiry_phase",
                processed_count=delivery_expired_result.processed_count,
                changed_count=delivery_expired_result.changed_count,
                skipped_count=delivery_expired_result.skipped_count,
                batch_size=self._delivery_expiry_batch_size,
                delivery_expiry_days=self._delivery_expiry_days,
            )
            unlock_result = await self._process_unlocks()
            self._logger.info(
                "order_tracker_unlock_phase",
                processed_count=unlock_result.processed_count,
                changed_count=unlock_result.changed_count,
                skipped_count=unlock_result.skipped_count,
                batch_size=self._unlock_batch_size,
                unlock_days=self._unlock_days,
            )
            return OrderTrackerRunResult(
                lock_acquired=True,
                reservation_expiry_processed_count=reservation_result.processed_count,
                reservation_expiry_changed_count=reservation_result.expired_count,
                wb_processed_count=wb_result.processed_count,
                wb_pickup_count=wb_result.pickup_count,
                wb_return_cancelled_count=wb_result.return_cancelled_count,
                wb_return_ignored_after_unlock_count=wb_result.return_ignored_after_unlock_count,
                delivery_expired_processed_count=delivery_expired_result.processed_count,
                delivery_expired_changed_count=delivery_expired_result.changed_count,
                unlock_processed_count=unlock_result.processed_count,
                unlock_changed_count=unlock_result.changed_count,
            )
        finally:
            await self._release_lock()

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

    async def _process_expired_reservations(self) -> ReservationExpiryResult:
        return await self._buyer_service.process_expired_reservations(
            batch_size=self._reservation_expiry_batch_size
        )

    async def _process_wb_events(self) -> WbEventProcessResult:
        candidates = await self._list_wb_event_candidates()
        pickup_count = 0
        pickup_skipped_count = 0
        return_cancelled_count = 0
        return_ignored_after_unlock_count = 0
        return_skipped_count = 0

        for candidate in candidates:
            sale_unlock_at = (
                candidate.sale_at + timedelta(days=self._unlock_days)
                if candidate.sale_at is not None
                else None
            )
            current_status = candidate.assignment_status

            if candidate.sale_at is not None and current_status == "order_verified":
                if await self._mark_assignment_picked_up(
                    assignment_id=candidate.assignment_id,
                    pickup_at=candidate.sale_at,
                ):
                    pickup_count += 1
                    current_status = "picked_up_wait_unlock"
                else:
                    pickup_skipped_count += 1
            elif candidate.sale_at is not None and current_status != "order_verified":
                pickup_skipped_count += 1

            if candidate.return_at is None:
                continue

            if sale_unlock_at is not None:
                if candidate.return_at > sale_unlock_at:
                    return_ignored_after_unlock_count += 1
                    continue
            elif current_status in {"picked_up_wait_review", "picked_up_wait_unlock"}:
                if candidate.unlock_at is None or candidate.return_at > candidate.unlock_at:
                    return_ignored_after_unlock_count += 1
                    continue

            if current_status not in {"order_verified", "picked_up_wait_review", "picked_up_wait_unlock"}:
                return_skipped_count += 1
                continue

            try:
                result = await self._finance_service.cancel_assignment_reservation(
                    assignment_id=candidate.assignment_id,
                    new_status="returned_within_14d",
                    seller_collateral_account_id=candidate.seller_collateral_account_id,
                    reward_reserved_account_id=candidate.reward_reserved_account_id,
                    idempotency_key=f"{_RETURN_IDEMPOTENCY_PREFIX}:{candidate.assignment_id}",
                    notification_event="assignment_returned",
                )
            except (InvalidStateError, NotFoundError):
                return_skipped_count += 1
                continue
            if result.changed:
                return_cancelled_count += 1
            else:
                return_skipped_count += 1

        return WbEventProcessResult(
            processed_count=len(candidates),
            pickup_count=pickup_count,
            pickup_skipped_count=pickup_skipped_count,
            return_cancelled_count=return_cancelled_count,
            return_ignored_after_unlock_count=return_ignored_after_unlock_count,
            return_skipped_count=return_skipped_count,
        )

    async def _list_wb_event_candidates(self) -> list[_WbEventCandidate]:
        async def operation(conn: AsyncConnection) -> list[_WbEventCandidate]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id AS assignment_id,
                        a.status AS assignment_status,
                        a.unlock_at,
                        sc.id AS seller_collateral_account_id,
                        rr.id AS reward_reserved_account_id,
                        sale.event_at AS sale_at,
                        ret.event_at AS return_at
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    JOIN accounts sc
                        ON sc.account_code = (
                            'user:' || l.seller_user_id::text || ':seller_collateral'
                        )
                    JOIN accounts rr
                        ON rr.account_code = 'system:reward_reserved'
                    LEFT JOIN LATERAL (
                        SELECT
                            COALESCE(wr.sale_dt, wr.order_dt, wr.create_dt) AS event_at,
                            wr.rrd_id
                        FROM wb_report_rows wr
                        WHERE wr.shop_id = l.shop_id
                          AND wr.nm_id = l.wb_product_id
                          AND wr.supplier_oper_name = %s
                          AND (
                                wr.wb_srid = a.order_id
                                OR wr.order_uid = a.order_id
                                OR split_part(wr.wb_srid, '.', 2) = a.order_id
                          )
                        ORDER BY
                            COALESCE(wr.sale_dt, wr.order_dt, wr.create_dt) DESC NULLS LAST,
                            wr.rrd_id DESC
                        LIMIT 1
                    ) sale ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            COALESCE(wr.sale_dt, wr.order_dt, wr.create_dt) AS event_at,
                            wr.rrd_id
                        FROM wb_report_rows wr
                        WHERE wr.shop_id = l.shop_id
                          AND wr.nm_id = l.wb_product_id
                          AND wr.supplier_oper_name = %s
                          AND (
                                wr.wb_srid = a.order_id
                                OR wr.order_uid = a.order_id
                                OR split_part(wr.wb_srid, '.', 2) = a.order_id
                          )
                        ORDER BY
                            COALESCE(wr.sale_dt, wr.order_dt, wr.create_dt) DESC NULLS LAST,
                            wr.rrd_id DESC
                        LIMIT 1
                    ) ret ON TRUE
                    WHERE a.status IN ('order_verified', 'picked_up_wait_review', 'picked_up_wait_unlock')
                      AND a.order_id IS NOT NULL
                      AND (sale.event_at IS NOT NULL OR ret.event_at IS NOT NULL)
                    ORDER BY a.updated_at ASC, a.id ASC
                    LIMIT %s
                    """,
                    (_PICKUP_OPERATION, _RETURN_OPERATION, self._wb_event_batch_size),
                )
                rows = await cur.fetchall()
                return [
                    _WbEventCandidate(
                        assignment_id=row["assignment_id"],
                        assignment_status=row["assignment_status"],
                        seller_collateral_account_id=row["seller_collateral_account_id"],
                        reward_reserved_account_id=row["reward_reserved_account_id"],
                        sale_at=row["sale_at"],
                        return_at=row["return_at"],
                        unlock_at=row["unlock_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def _mark_assignment_picked_up(self, *, assignment_id: int, pickup_at: datetime) -> bool:
        pickup_at_utc = (
            pickup_at.astimezone(UTC) if pickup_at.tzinfo else pickup_at.replace(tzinfo=UTC)
        )
        unlock_at = pickup_at_utc + timedelta(days=self._unlock_days)

        async def operation(conn: AsyncConnection) -> bool:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.status,
                        a.review_required,
                        l.review_phrases
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    WHERE a.id = %s
                    FOR UPDATE OF a, l
                    """,
                    (assignment_id,),
                )
                assignment = await cur.fetchone()
                if assignment is None or assignment["status"] != "order_verified":
                    return False
                next_status = (
                    "picked_up_wait_review" if assignment["review_required"] else "picked_up_wait_unlock"
                )
                review_phrases = []
                if assignment["review_required"]:
                    review_phrases = self._pick_assignment_review_phrases(
                        list(assignment["review_phrases"] or [])
                    )
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = %s,
                        pickup_at = COALESCE(pickup_at, %s),
                        unlock_at = COALESCE(unlock_at, %s),
                        review_phrases = %s,
                        cancel_reason = NULL,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                      AND status = 'order_verified'
                    """,
                    (next_status, pickup_at_utc, unlock_at, review_phrases, assignment_id),
                )
                changed = cur.rowcount == 1
                if changed:
                    await self._notifications.enqueue_assignment_picked_up_locked(
                        cur,
                        assignment_id=assignment_id,
                    )
                return changed

        return await run_in_transaction(self._pool, operation)

    async def _process_delivery_expired(self) -> BatchProcessResult:
        candidates = await self._list_delivery_expired_candidates()
        changed_count = 0
        skipped_count = 0

        for candidate in candidates:
            try:
                result = await self._finance_service.cancel_assignment_reservation(
                    assignment_id=candidate.assignment_id,
                    new_status="delivery_expired",
                    seller_collateral_account_id=candidate.seller_collateral_account_id,
                    reward_reserved_account_id=candidate.reward_reserved_account_id,
                    idempotency_key=f"{_DELIVERY_EXPIRED_IDEMPOTENCY_PREFIX}:{candidate.assignment_id}",
                    notification_event="delivery_expired",
                )
            except (InvalidStateError, NotFoundError):
                skipped_count += 1
                continue
            if result.changed:
                changed_count += 1
            else:
                skipped_count += 1

        return BatchProcessResult(
            processed_count=len(candidates),
            changed_count=changed_count,
            skipped_count=skipped_count,
        )

    async def _list_delivery_expired_candidates(self) -> list[_DeliveryExpiredCandidate]:
        async def operation(conn: AsyncConnection) -> list[_DeliveryExpiredCandidate]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id AS assignment_id,
                        sc.id AS seller_collateral_account_id,
                        rr.id AS reward_reserved_account_id
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    JOIN accounts sc
                        ON sc.account_code = (
                            'user:' || l.seller_user_id::text || ':seller_collateral'
                        )
                    JOIN accounts rr
                        ON rr.account_code = 'system:reward_reserved'
                    WHERE a.status = 'order_verified'
                      AND COALESCE(a.order_submitted_at, a.created_at)
                          <= timezone('utc', now()) - (%s * interval '1 day')
                    ORDER BY
                        COALESCE(a.order_submitted_at, a.created_at) ASC,
                        a.id ASC
                    LIMIT %s
                    """,
                    (self._delivery_expiry_days, self._delivery_expiry_batch_size),
                )
                rows = await cur.fetchall()
                return [
                    _DeliveryExpiredCandidate(
                        assignment_id=row["assignment_id"],
                        seller_collateral_account_id=row["seller_collateral_account_id"],
                        reward_reserved_account_id=row["reward_reserved_account_id"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def _process_unlocks(self) -> BatchProcessResult:
        candidates = await self._list_unlock_candidates()
        changed_count = 0
        skipped_count = 0

        for candidate in candidates:
            buyer_available_account_id = await self._ensure_buyer_available_account_id(
                buyer_user_id=candidate.buyer_user_id
            )
            try:
                result = await self._finance_service.unlock_assignment_reward(
                    assignment_id=candidate.assignment_id,
                    buyer_available_account_id=buyer_available_account_id,
                    reward_reserved_account_id=candidate.reward_reserved_account_id,
                    idempotency_key=f"{_UNLOCK_IDEMPOTENCY_PREFIX}:{candidate.assignment_id}",
                )
            except (InvalidStateError, NotFoundError):
                skipped_count += 1
                continue
            if result.changed:
                changed_count += 1
            else:
                skipped_count += 1

        return BatchProcessResult(
            processed_count=len(candidates),
            changed_count=changed_count,
            skipped_count=skipped_count,
        )

    async def _list_unlock_candidates(self) -> list[_UnlockCandidate]:
        async def operation(conn: AsyncConnection) -> list[_UnlockCandidate]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id AS assignment_id,
                        a.buyer_user_id,
                        rr.id AS reward_reserved_account_id
                    FROM assignments a
                    JOIN accounts rr
                        ON rr.account_code = 'system:reward_reserved'
                    WHERE a.status = 'picked_up_wait_unlock'
                      AND a.unlock_at IS NOT NULL
                      AND a.unlock_at <= timezone('utc', now())
                    ORDER BY a.unlock_at ASC, a.id ASC
                    LIMIT %s
                    """,
                    (self._unlock_batch_size,),
                )
                rows = await cur.fetchall()
                return [
                    _UnlockCandidate(
                        assignment_id=row["assignment_id"],
                        buyer_user_id=row["buyer_user_id"],
                        reward_reserved_account_id=row["reward_reserved_account_id"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    @staticmethod
    def _pick_assignment_review_phrases(review_phrases: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for phrase in review_phrases:
            item = str(phrase).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        if len(normalized) <= 2:
            return normalized
        return random.sample(normalized, k=2)

    async def _ensure_buyer_available_account_id(self, *, buyer_user_id: int) -> int:
        async def operation(conn: AsyncConnection) -> int:
            account_code = f"user:{buyer_user_id}:buyer_available"
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO accounts (
                        owner_user_id,
                        account_code,
                        account_kind
                    )
                    VALUES (%s, %s, 'buyer_available')
                    ON CONFLICT (account_code)
                    DO UPDATE SET
                        owner_user_id = EXCLUDED.owner_user_id,
                        updated_at = timezone('utc', now())
                    RETURNING id
                    """,
                    (buyer_user_id, account_code),
                )
                row: dict[str, Any] = await cur.fetchone()
                return int(row["id"])

        return await run_in_transaction(self._pool, operation)
