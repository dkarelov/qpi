from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.errors import (
    DuplicateOrderError,
    InvalidStateError,
    NoSlotsAvailableError,
    NotFoundError,
    PayloadValidationError,
)
from libs.domain.ledger import FinanceService
from libs.domain.models import (
    AdminPurchaseReviewVerificationResult,
    DeleteExecutionResult,
    DeletePreview,
    PurchaseExpiryResult,
    PurchaseOrderSubmitResult,
    PurchaseReservationResult,
    PurchaseReviewSubmitResult,
    StatusChangeResult,
)
from libs.domain.notifications import NotificationService
from libs.domain.purchase_tokens import (
    DecodedPurchasePayload,
    DecodedReviewPayload,
    decode_purchase_payload,
    decode_review_payload,
)


class PurchaseStatus(StrEnum):
    RESERVED = "reserved"
    ORDER_VERIFIED = "order_verified"
    PICKED_UP_WAIT_REVIEW = "picked_up_wait_review"
    PICKED_UP_WAIT_UNLOCK = "picked_up_wait_unlock"
    WITHDRAW_SENT = "withdraw_sent"
    EXPIRED = "expired_2h"
    BUYER_CANCELLED = "buyer_cancelled"
    WB_INVALID = "wb_invalid"
    RETURNED_WITHIN_14D = "returned_within_14d"
    DELIVERY_EXPIRED = "delivery_expired"


_ACTIVE_PURCHASE_STATES = (
    PurchaseStatus.RESERVED.value,
    PurchaseStatus.ORDER_VERIFIED.value,
    PurchaseStatus.PICKED_UP_WAIT_REVIEW.value,
    PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value,
    PurchaseStatus.WITHDRAW_SENT.value,
)
_OPEN_PURCHASE_STATES = (
    PurchaseStatus.RESERVED.value,
    PurchaseStatus.ORDER_VERIFIED.value,
    PurchaseStatus.PICKED_UP_WAIT_REVIEW.value,
    PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value,
)
_COLLATERAL_DEDUCTING_PURCHASE_STATES = (*_OPEN_PURCHASE_STATES, PurchaseStatus.WITHDRAW_SENT.value)
_ORDER_PAYLOAD_ALLOWED_STATES = {
    PurchaseStatus.RESERVED.value,
    PurchaseStatus.ORDER_VERIFIED.value,
}
_CANCELLATION_STATES = {
    PurchaseStatus.EXPIRED.value,
    PurchaseStatus.BUYER_CANCELLED.value,
    PurchaseStatus.WB_INVALID.value,
    PurchaseStatus.RETURNED_WITHIN_14D.value,
    PurchaseStatus.DELIVERY_EXPIRED.value,
}
_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX = "reservation-expire"
_ORDERED_AT_FUTURE_TOLERANCE = timedelta(minutes=15)
_REVIEW_STATUS_PENDING_MANUAL = "pending_manual"
_REVIEW_STATUS_VERIFIED_AUTO = "verified_auto"
_REVIEW_STATUS_VERIFIED_ADMIN = "verified_admin"
_REVIEW_NORMALIZE_WHITESPACE_RE = re.compile(r"\s+")
_MANUAL_SOURCE = "manual"


class PurchaseLifecycleService:
    """Purchase transitions, semantic cashback/collateral movement, and purchase notifications."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        finance_service: FinanceService | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        self._pool = pool
        self._finance = finance_service or FinanceService(pool)
        self._notifications = notification_service or NotificationService(pool)

    async def reserve_purchase(
        self,
        *,
        buyer_user_id: int,
        announcement_id: int,
        idempotency_seed: str,
        reservation_timeout_hours: int = 4,
        review_required: bool = True,
    ) -> PurchaseReservationResult:
        if reservation_timeout_hours < 1:
            raise ValueError("reservation_timeout_hours must be >= 1")

        async def operation(conn: AsyncConnection) -> PurchaseReservationResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                existing = await self._find_reservation_by_idempotency_locked(
                    cur,
                    idempotency_key=idempotency_seed,
                )
                if existing is not None:
                    return existing

                await self._ensure_buyer_user_exists_locked(cur, buyer_user_id=buyer_user_id)
                await cur.execute(
                    """
                    SELECT
                        l.id,
                        l.seller_user_id,
                        l.status,
                        l.reward_usdt,
                        l.wb_product_id,
                        l.available_slots,
                        l.deleted_at,
                        s.deleted_at AS shop_deleted_at
                    FROM listings l
                    JOIN shops s ON s.id = l.shop_id
                    WHERE l.id = %s
                    FOR UPDATE OF l
                    FOR SHARE OF s
                    """,
                    (announcement_id,),
                )
                announcement = await cur.fetchone()
                if announcement is None:
                    raise NotFoundError(f"listing {announcement_id} not found")
                if announcement["deleted_at"] is not None or announcement["shop_deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")
                if announcement["status"] != "active":
                    raise InvalidStateError("listing must be active for reservation")

                await self._ensure_buyer_has_not_purchased_item_locked(
                    cur,
                    buyer_user_id=buyer_user_id,
                    wb_product_id=int(announcement["wb_product_id"]),
                )
                if announcement["available_slots"] <= 0:
                    raise NoSlotsAvailableError("listing has no available slots")

                seller_collateral_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=int(announcement["seller_user_id"]),
                    account_kind="seller_collateral",
                )
                reward_reserved_account_id = await self._ensure_system_account(
                    cur,
                    account_kind="reward_reserved",
                )

                await cur.execute(
                    """
                    UPDATE listings
                    SET available_slots = available_slots - 1,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (announcement_id,),
                )

                cashback_usdt = _normalize_amount(announcement["reward_usdt"])
                task_uuid = uuid4()

                try:
                    await cur.execute(
                        """
                        INSERT INTO assignments (
                            listing_id,
                            buyer_user_id,
                            wb_product_id,
                            task_uuid,
                            status,
                            reward_usdt,
                            reservation_expires_at,
                            review_required,
                            idempotency_key
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            'reserved',
                            %s,
                            timezone('utc', now()) + (%s * interval '1 hour'),
                            %s,
                            %s
                        )
                        RETURNING id, reservation_expires_at
                        """,
                        (
                            announcement_id,
                            buyer_user_id,
                            announcement["wb_product_id"],
                            task_uuid,
                            cashback_usdt,
                            reservation_timeout_hours,
                            review_required,
                            idempotency_seed,
                        ),
                    )
                except UniqueViolation as exc:
                    constraint_name = exc.diag.constraint_name if exc.diag is not None else None
                    if constraint_name == "uq_assignments_buyer_product_active":
                        raise InvalidStateError("buyer already has assignment for this item") from exc
                    raise
                purchase = await cur.fetchone()

                await self._transfer_locked(
                    cur,
                    from_account_id=seller_collateral_account_id,
                    to_account_id=reward_reserved_account_id,
                    amount_usdt=cashback_usdt,
                    event_type="slot_reserve",
                    idempotency_key=_ledger_key(idempotency_seed),
                    entity_type="assignment",
                    entity_id=purchase["id"],
                    metadata={"assignment_id": purchase["id"], "listing_id": announcement_id},
                )

                await self._upsert_hold(
                    cur,
                    account_id=reward_reserved_account_id,
                    hold_type="slot_reserve",
                    status="active",
                    amount_usdt=cashback_usdt,
                    listing_id=announcement_id,
                    assignment_id=purchase["id"],
                    idempotency_key=_hold_key(idempotency_seed),
                )

                return PurchaseReservationResult(
                    purchase_id=purchase["id"],
                    created=True,
                    cashback_usdt=cashback_usdt,
                    reservation_expires_at=purchase["reservation_expires_at"],
                    task_uuid=task_uuid,
                )

        return await run_in_transaction(self._pool, operation)

    async def submit_order_proof(
        self,
        *,
        buyer_user_id: int,
        purchase_id: int,
        token_payload: str,
    ) -> PurchaseOrderSubmitResult:
        decoded = decode_purchase_payload(token_payload)

        async def operation(conn: AsyncConnection) -> PurchaseOrderSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._submit_order_proof_decoded_locked(
                    cur,
                    buyer_user_id=buyer_user_id,
                    purchase_id=purchase_id,
                    decoded=decoded,
                )

        return await run_in_transaction(self._pool, operation)

    async def submit_order_proof_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        token_payload: str,
    ) -> PurchaseOrderSubmitResult:
        decoded = decode_purchase_payload(token_payload)

        async def operation(conn: AsyncConnection) -> PurchaseOrderSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM assignments
                    WHERE buyer_user_id = %s
                      AND task_uuid = %s
                    """,
                    (buyer_user_id, decoded.task_uuid),
                )
                purchase = await cur.fetchone()
                if purchase is None:
                    raise NotFoundError("assignment not found for payload")
                return await self._submit_order_proof_decoded_locked(
                    cur,
                    buyer_user_id=buyer_user_id,
                    purchase_id=purchase["id"],
                    decoded=decoded,
                )

        return await run_in_transaction(self._pool, operation)

    async def submit_review_confirmation(
        self,
        *,
        buyer_user_id: int,
        purchase_id: int,
        token_payload: str,
    ) -> PurchaseReviewSubmitResult:
        decoded = decode_review_payload(token_payload)

        async def operation(conn: AsyncConnection) -> PurchaseReviewSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                purchase = await self._load_review_purchase_locked(cur, purchase_id=purchase_id)
                if purchase is None:
                    raise NotFoundError(f"assignment {purchase_id} not found")
                if purchase["buyer_user_id"] != buyer_user_id:
                    raise NotFoundError(f"assignment {purchase_id} not found for buyer")
                return await self._store_review_confirmation_locked(
                    cur,
                    purchase=purchase,
                    decoded=decoded,
                    source="plugin_base64",
                )

        return await run_in_transaction(self._pool, operation)

    async def submit_review_confirmation_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        token_payload: str,
    ) -> PurchaseReviewSubmitResult:
        decoded = decode_review_payload(token_payload)

        async def operation(conn: AsyncConnection) -> PurchaseReviewSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                purchase = await self._load_review_purchase_by_task_uuid_locked(
                    cur,
                    buyer_user_id=buyer_user_id,
                    task_uuid=decoded.task_uuid,
                )
                if purchase is None:
                    raise NotFoundError("assignment not found for payload")
                return await self._store_review_confirmation_locked(
                    cur,
                    purchase=purchase,
                    decoded=decoded,
                    source="plugin_base64",
                )

        return await run_in_transaction(self._pool, operation)

    async def admin_verify_review_confirmation(
        self,
        *,
        admin_user_id: int,
        purchase_id: int,
        token_payload: str,
        idempotency_seed: str,
    ) -> AdminPurchaseReviewVerificationResult:
        decoded = decode_review_payload(token_payload)

        async def operation(conn: AsyncConnection) -> AdminPurchaseReviewVerificationResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                purchase = await self._load_review_purchase_locked(cur, purchase_id=purchase_id)
                if purchase is None:
                    raise NotFoundError(f"assignment {purchase_id} not found")
                result = await self._store_review_confirmation_locked(
                    cur,
                    purchase=purchase,
                    decoded=decoded,
                    source="admin_base64",
                    admin_user_id=admin_user_id,
                    idempotency_seed=idempotency_seed,
                )
                return AdminPurchaseReviewVerificationResult(
                    purchase_id=result.purchase_id,
                    changed=result.changed,
                    status=result.status,
                    task_uuid=result.task_uuid,
                    wb_product_id=result.wb_product_id,
                    reviewed_at=result.reviewed_at,
                    rating=result.rating,
                    review_text=result.review_text,
                    verification_status=result.verification_status,
                )

        return await run_in_transaction(self._pool, operation)

    async def cancel_reserved_purchase_by_buyer(
        self,
        *,
        buyer_user_id: int,
        purchase_id: int,
        idempotency_seed: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id,
                        a.status,
                        a.buyer_user_id
                    FROM assignments a
                    WHERE a.id = %s
                    FOR UPDATE OF a
                    """,
                    (purchase_id,),
                )
                purchase = await cur.fetchone()
                if purchase is None or purchase["buyer_user_id"] != buyer_user_id:
                    raise NotFoundError(f"assignment {purchase_id} not found for buyer")
                if purchase["status"] in {PurchaseStatus.EXPIRED.value, PurchaseStatus.BUYER_CANCELLED.value}:
                    return StatusChangeResult(changed=False)
                if purchase["status"] != PurchaseStatus.RESERVED.value:
                    raise InvalidStateError("assignment cannot be cancelled in current state")
                return await self._cancel_purchase_to_status_locked(
                    cur,
                    purchase_id=purchase_id,
                    new_status=PurchaseStatus.BUYER_CANCELLED,
                    idempotency_seed=idempotency_seed,
                    notification_event=None,
                )

        return await run_in_transaction(self._pool, operation)

    async def process_expired_reservations(self, *, batch_size: int = 100) -> PurchaseExpiryResult:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        async def list_operation(conn: AsyncConnection) -> list[dict[str, Any]]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT a.id AS purchase_id
                    FROM assignments a
                    WHERE a.status = 'reserved'
                      AND a.reservation_expires_at <= timezone('utc', now())
                    ORDER BY a.reservation_expires_at ASC, a.id ASC
                    LIMIT %s
                    """,
                    (batch_size,),
                )
                return list(await cur.fetchall())

        candidates = await run_in_transaction(self._pool, list_operation, read_only=True)

        expired_count = 0
        for row in candidates:
            try:
                result = await self.expire_reserved_purchase(
                    purchase_id=row["purchase_id"],
                    idempotency_seed=f"{_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX}:{row['purchase_id']}",
                )
            except (InvalidStateError, NotFoundError):
                continue
            if result.changed:
                expired_count += 1

        return PurchaseExpiryResult(
            processed_count=len(candidates),
            expired_count=expired_count,
        )

    async def expire_reserved_purchase(self, *, purchase_id: int, idempotency_seed: str) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._cancel_purchase_to_status_locked(
                    cur,
                    purchase_id=purchase_id,
                    new_status=PurchaseStatus.EXPIRED,
                    idempotency_seed=idempotency_seed,
                    notification_event="reservation_expired",
                )

        return await run_in_transaction(self._pool, operation)

    async def mark_picked_up(
        self,
        *,
        purchase_id: int,
        pickup_at: datetime,
        unlock_days: int,
    ) -> bool:
        if unlock_days < 1:
            raise ValueError("unlock_days must be >= 1")
        pickup_at_utc = pickup_at.astimezone(UTC) if pickup_at.tzinfo else pickup_at.replace(tzinfo=UTC)
        unlock_at = pickup_at_utc + timedelta(days=unlock_days)

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
                    (purchase_id,),
                )
                purchase = await cur.fetchone()
                if purchase is None or purchase["status"] != PurchaseStatus.ORDER_VERIFIED.value:
                    return False
                next_status = (
                    PurchaseStatus.PICKED_UP_WAIT_REVIEW.value
                    if purchase["review_required"]
                    else PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value
                )
                review_phrases = []
                if purchase["review_required"]:
                    review_phrases = self._pick_purchase_review_phrases(list(purchase["review_phrases"] or []))
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
                    (next_status, pickup_at_utc, unlock_at, review_phrases, purchase_id),
                )
                changed = cur.rowcount == 1
                if changed:
                    await self._notifications.enqueue_assignment_picked_up_locked(cur, assignment_id=purchase_id)
                return changed

        return await run_in_transaction(self._pool, operation)

    async def mark_returned_within_unlock_window(
        self,
        *,
        purchase_id: int,
        idempotency_seed: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._cancel_purchase_to_status_locked(
                    cur,
                    purchase_id=purchase_id,
                    new_status=PurchaseStatus.RETURNED_WITHIN_14D,
                    idempotency_seed=idempotency_seed,
                    notification_event="assignment_returned",
                )

        return await run_in_transaction(self._pool, operation)

    async def expire_delivery(self, *, purchase_id: int, idempotency_seed: str) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._cancel_purchase_to_status_locked(
                    cur,
                    purchase_id=purchase_id,
                    new_status=PurchaseStatus.DELIVERY_EXPIRED,
                    idempotency_seed=idempotency_seed,
                    notification_event="delivery_expired",
                )

        return await run_in_transaction(self._pool, operation)

    async def unlock_cashback(self, *, purchase_id: int, idempotency_seed: str) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, buyer_user_id, reward_usdt, status, unlock_at
                    FROM assignments
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (purchase_id,),
                )
                purchase = await cur.fetchone()
                if purchase is None:
                    raise NotFoundError(f"assignment {purchase_id} not found")
                if purchase["status"] == PurchaseStatus.WITHDRAW_SENT.value:
                    return StatusChangeResult(changed=False)
                if purchase["status"] != PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value:
                    raise InvalidStateError("assignment must be in picked_up_wait_unlock state")
                if purchase["unlock_at"] is None:
                    raise InvalidStateError("assignment unlock_at is not set")

                await cur.execute("SELECT now() AS current_time")
                now_row = await cur.fetchone()
                if purchase["unlock_at"] > now_row["current_time"]:
                    raise InvalidStateError("assignment unlock time has not passed yet")

                buyer_available_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=int(purchase["buyer_user_id"]),
                    account_kind="buyer_available",
                )
                reward_reserved_account_id = await self._ensure_system_account(
                    cur,
                    account_kind="reward_reserved",
                )

                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'withdraw_sent',
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (purchase_id,),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=reward_reserved_account_id,
                    to_account_id=buyer_available_account_id,
                    amount_usdt=_normalize_amount(purchase["reward_usdt"]),
                    event_type="reward_unlock",
                    idempotency_key=_ledger_key(idempotency_seed),
                    entity_type="assignment",
                    entity_id=purchase_id,
                    metadata={"assignment_id": purchase_id},
                )

                await cur.execute(
                    """
                    UPDATE balance_holds
                    SET status = 'consumed',
                        released_at = timezone('utc', now())
                    WHERE assignment_id = %s
                        AND hold_type = 'slot_reserve'
                        AND status = 'active'
                    """,
                    (purchase_id,),
                )

                await self._notifications.enqueue_assignment_reward_unlocked_locked(cur, assignment_id=purchase_id)
                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def delete_announcement_locked(
        self,
        cur,
        *,
        seller_user_id: int,
        announcement_id: int,
        deleted_by_user_id: int,
        idempotency_seed: str,
        buyer_payout_aggregates: dict[int, dict[str, Any]] | None = None,
    ) -> DeleteExecutionResult:
        await cur.execute(
            """
            SELECT l.id, l.deleted_at, s.title AS shop_title
            FROM listings l
            JOIN shops s ON s.id = l.shop_id
            WHERE l.id = %s
              AND l.seller_user_id = %s
            FOR UPDATE
            """,
            (announcement_id, seller_user_id),
        )
        announcement = await cur.fetchone()
        if announcement is None:
            raise NotFoundError(f"listing {announcement_id} not found")
        if announcement["deleted_at"] is not None:
            return DeleteExecutionResult(
                changed=False,
                assignment_transfers_count=0,
                assignment_transferred_usdt=Decimal("0.000000"),
                unassigned_collateral_returned_usdt=Decimal("0.000000"),
            )

        seller_available_account_id = await self._ensure_owner_account(
            cur,
            owner_user_id=seller_user_id,
            account_kind="seller_available",
        )
        seller_collateral_account_id = await self._ensure_owner_account(
            cur,
            owner_user_id=seller_user_id,
            account_kind="seller_collateral",
        )
        reward_reserved_account_id = await self._ensure_system_account(cur, account_kind="reward_reserved")

        await cur.execute(
            """
            SELECT
                h.id,
                h.assignment_id,
                h.amount_usdt,
                a.buyer_user_id,
                u.telegram_id AS buyer_telegram_id
            FROM balance_holds h
            JOIN assignments a ON a.id = h.assignment_id
            JOIN users u ON u.id = a.buyer_user_id
            WHERE h.listing_id = %s
              AND h.hold_type = 'slot_reserve'
              AND h.status = 'active'
            ORDER BY h.id ASC
            FOR UPDATE OF h, a
            """,
            (announcement_id,),
        )
        active_slot_holds = await cur.fetchall()
        assigned_reward_deduction = await self.load_announcement_collateral_deduction_locked(
            cur,
            announcement_id=announcement_id,
        )

        purchase_transfers_count = 0
        purchase_transferred_usdt = Decimal("0.000000")
        local_buyer_aggregates = buyer_payout_aggregates if buyer_payout_aggregates is not None else {}
        for hold in active_slot_holds:
            buyer_available_account_id = await self._ensure_owner_account(
                cur,
                owner_user_id=hold["buyer_user_id"],
                account_kind="buyer_available",
            )
            amount = _normalize_amount(hold["amount_usdt"])
            transfer_key = f"{idempotency_seed}:assignment:{hold['assignment_id']}:hold:{hold['id']}"
            transfer_result = await self._transfer_locked(
                cur,
                from_account_id=reward_reserved_account_id,
                to_account_id=buyer_available_account_id,
                amount_usdt=amount,
                event_type="listing_delete_assignment_release",
                idempotency_key=_ledger_key(transfer_key),
                entity_type="assignment",
                entity_id=hold["assignment_id"],
                metadata={
                    "listing_id": announcement_id,
                    "assignment_id": hold["assignment_id"],
                    "hold_id": hold["id"],
                },
            )
            if transfer_result.created:
                purchase_transfers_count += 1
                purchase_transferred_usdt += amount
                aggregate = local_buyer_aggregates.setdefault(
                    int(hold["buyer_user_id"]),
                    {
                        "telegram_id": int(hold["buyer_telegram_id"]),
                        "item_count": 0,
                        "total_reward_usdt": Decimal("0.000000"),
                    },
                )
                aggregate["item_count"] += 1
                aggregate["total_reward_usdt"] += amount

            await cur.execute(
                """
                UPDATE assignments
                SET status = 'withdraw_sent',
                    updated_at = timezone('utc', now())
                WHERE id = %s
                  AND status <> 'withdraw_sent'
                """,
                (hold["assignment_id"],),
            )
            await cur.execute(
                """
                UPDATE balance_holds
                SET status = 'consumed',
                    released_at = timezone('utc', now())
                WHERE id = %s
                  AND status = 'active'
                """,
                (hold["id"],),
            )

        await cur.execute(
            """
            SELECT id, amount_usdt
            FROM balance_holds
            WHERE listing_id = %s
              AND hold_type = 'collateral'
              AND status = 'active'
            ORDER BY id ASC
            FOR UPDATE
            """,
            (announcement_id,),
        )
        collateral_holds = await cur.fetchall()
        collateral_sum = Decimal("0.000000")
        for hold in collateral_holds:
            collateral_sum += _normalize_amount(hold["amount_usdt"])

        unassigned_collateral = collateral_sum - assigned_reward_deduction
        if unassigned_collateral < Decimal("0.000000"):
            unassigned_collateral = Decimal("0.000000")

        unassigned_collateral = _normalize_amount(unassigned_collateral)
        if unassigned_collateral > Decimal("0.000000"):
            await self._transfer_locked(
                cur,
                from_account_id=seller_collateral_account_id,
                to_account_id=seller_available_account_id,
                amount_usdt=unassigned_collateral,
                event_type="listing_delete_collateral_return",
                idempotency_key=_ledger_key(f"{idempotency_seed}:collateral"),
                entity_type="listing",
                entity_id=announcement_id,
                metadata={
                    "listing_id": announcement_id,
                    "total_collateral": str(_normalize_amount(collateral_sum)),
                    "assigned_reward_deduction_usdt": str(_normalize_amount(assigned_reward_deduction)),
                    "assignment_transferred_usdt": str(_normalize_amount(purchase_transferred_usdt)),
                },
            )

        if collateral_holds:
            hold_ids = [row["id"] for row in collateral_holds]
            await cur.execute(
                """
                UPDATE balance_holds
                SET status = 'consumed',
                    released_at = timezone('utc', now())
                WHERE id = ANY(%s)
                  AND status = 'active'
                """,
                (hold_ids,),
            )

        await cur.execute(
            """
            UPDATE listings
            SET status = 'paused',
                paused_at = timezone('utc', now()),
                pause_reason = 'deleted_by_seller',
                pause_source = %s,
                deleted_at = timezone('utc', now()),
                deleted_by_user_id = %s,
                updated_at = timezone('utc', now())
            WHERE id = %s
            """,
            (_MANUAL_SOURCE, deleted_by_user_id, announcement_id),
        )

        if buyer_payout_aggregates is None and local_buyer_aggregates:
            await self.enqueue_buyer_early_payout_notifications_locked(
                cur,
                scope="listing",
                scope_id=announcement_id,
                shop_title=announcement["shop_title"],
                aggregates=local_buyer_aggregates,
            )

        return DeleteExecutionResult(
            changed=True,
            assignment_transfers_count=purchase_transfers_count,
            assignment_transferred_usdt=_normalize_amount(purchase_transferred_usdt),
            unassigned_collateral_returned_usdt=unassigned_collateral,
        )

    async def load_announcement_delete_preview_locked(self, cur, *, announcement_id: int) -> DeletePreview:
        await cur.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM assignments
                    WHERE listing_id = %s
                      AND status = ANY(%s)
                ) AS open_assignments_count,
                (
                    SELECT COALESCE(SUM(amount_usdt), 0)
                    FROM balance_holds
                    WHERE listing_id = %s
                      AND hold_type = 'slot_reserve'
                      AND status = 'active'
                ) AS assignment_linked_reserved_usdt,
                (
                    SELECT COALESCE(SUM(amount_usdt), 0)
                    FROM balance_holds
                    WHERE listing_id = %s
                      AND hold_type = 'collateral'
                      AND status = 'active'
                ) AS collateral_usdt
            """,
            (announcement_id, list(_OPEN_PURCHASE_STATES), announcement_id, announcement_id),
        )
        row = await cur.fetchone()
        assignment_linked_reserved = _normalize_amount(row["assignment_linked_reserved_usdt"])
        collateral = _normalize_amount(row["collateral_usdt"])
        assigned_reward_deduction = await self.load_announcement_collateral_deduction_locked(
            cur,
            announcement_id=announcement_id,
        )
        unassigned_collateral = collateral - assigned_reward_deduction
        if unassigned_collateral < Decimal("0.000000"):
            unassigned_collateral = Decimal("0.000000")

        return DeletePreview(
            active_listings_count=1,
            open_assignments_count=row["open_assignments_count"],
            assignment_linked_reserved_usdt=assignment_linked_reserved,
            unassigned_collateral_usdt=_normalize_amount(unassigned_collateral),
        )

    async def load_announcement_collateral_deduction_locked(self, cur, *, announcement_id: int) -> Decimal:
        await cur.execute(
            """
            SELECT COALESCE(SUM(reward_usdt), 0) AS assigned_reward_usdt
            FROM assignments
            WHERE listing_id = %s
              AND status = ANY(%s)
            """,
            (announcement_id, list(_COLLATERAL_DEDUCTING_PURCHASE_STATES)),
        )
        row = await cur.fetchone()
        return _normalize_amount(row["assigned_reward_usdt"])

    async def load_shop_delete_preview_locked(self, cur, *, shop_id: int) -> DeletePreview:
        await cur.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM listings
                    WHERE shop_id = %s
                      AND deleted_at IS NULL
                      AND status = 'active'
                ) AS active_listings_count,
                (
                    SELECT COUNT(*)
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    WHERE l.shop_id = %s
                      AND l.deleted_at IS NULL
                      AND a.status = ANY(%s)
                ) AS open_assignments_count,
                (
                    SELECT COALESCE(SUM(h.amount_usdt), 0)
                    FROM balance_holds h
                    JOIN listings l ON l.id = h.listing_id
                    WHERE l.shop_id = %s
                      AND l.deleted_at IS NULL
                      AND h.hold_type = 'slot_reserve'
                      AND h.status = 'active'
                ) AS assignment_linked_reserved_usdt,
                (
                    SELECT COALESCE(SUM(h.amount_usdt), 0)
                    FROM balance_holds h
                    JOIN listings l ON l.id = h.listing_id
                    WHERE l.shop_id = %s
                      AND l.deleted_at IS NULL
                      AND h.hold_type = 'collateral'
                      AND h.status = 'active'
                ) AS collateral_usdt
            """,
            (shop_id, shop_id, list(_OPEN_PURCHASE_STATES), shop_id, shop_id),
        )
        row = await cur.fetchone()

        assignment_linked_reserved = _normalize_amount(row["assignment_linked_reserved_usdt"])
        collateral = _normalize_amount(row["collateral_usdt"])
        await cur.execute(
            """
            SELECT COALESCE(SUM(a.reward_usdt), 0) AS assigned_reward_usdt
            FROM assignments a
            JOIN listings l ON l.id = a.listing_id
            WHERE l.shop_id = %s
              AND l.deleted_at IS NULL
              AND a.status = ANY(%s)
            """,
            (shop_id, list(_COLLATERAL_DEDUCTING_PURCHASE_STATES)),
        )
        deduction_row = await cur.fetchone()
        assigned_reward_deduction = _normalize_amount(deduction_row["assigned_reward_usdt"])
        unassigned_collateral = collateral - assigned_reward_deduction
        if unassigned_collateral < Decimal("0.000000"):
            unassigned_collateral = Decimal("0.000000")

        return DeletePreview(
            active_listings_count=row["active_listings_count"],
            open_assignments_count=row["open_assignments_count"],
            assignment_linked_reserved_usdt=assignment_linked_reserved,
            unassigned_collateral_usdt=_normalize_amount(unassigned_collateral),
        )

    async def enqueue_buyer_early_payout_notifications_locked(
        self,
        cur,
        *,
        scope: str,
        scope_id: int,
        shop_title: str,
        aggregates: dict[int, dict[str, Any]],
    ) -> None:
        for aggregate in aggregates.values():
            await self._notifications.enqueue_buyer_early_payout_locked(
                cur,
                buyer_telegram_id=int(aggregate["telegram_id"]),
                scope=scope,
                scope_id=scope_id,
                shop_title=shop_title,
                item_count=int(aggregate["item_count"]),
                total_reward_usdt=_normalize_amount(aggregate["total_reward_usdt"]),
            )

    async def _submit_order_proof_decoded_locked(
        self,
        cur,
        *,
        buyer_user_id: int,
        purchase_id: int,
        decoded: DecodedPurchasePayload,
    ) -> PurchaseOrderSubmitResult:
        await cur.execute(
            """
            SELECT
                a.id,
                a.listing_id,
                a.buyer_user_id,
                a.status,
                a.task_uuid,
                a.order_id,
                a.reservation_expires_at,
                l.wb_product_id
            FROM assignments a
            JOIN listings l ON l.id = a.listing_id
            WHERE a.id = %s
            FOR UPDATE OF a, l
            """,
            (purchase_id,),
        )
        purchase = await cur.fetchone()
        if purchase is None:
            raise NotFoundError(f"assignment {purchase_id} not found")
        if purchase["buyer_user_id"] != buyer_user_id:
            raise NotFoundError(f"assignment {purchase_id} not found for buyer")
        if purchase["status"] not in _ORDER_PAYLOAD_ALLOWED_STATES:
            raise InvalidStateError("assignment cannot accept payload in current state")
        if decoded.task_uuid != purchase["task_uuid"]:
            raise PayloadValidationError("payload field 'task_uuid' does not match assignment")

        await cur.execute("SELECT now() AS current_time")
        now_row = await cur.fetchone()
        current_time = now_row["current_time"]
        if decoded.ordered_at > current_time + _ORDERED_AT_FUTURE_TOLERANCE:
            raise PayloadValidationError("payload field 'ordered_at' cannot be in the future")
        if purchase["status"] == PurchaseStatus.RESERVED.value and purchase["reservation_expires_at"] <= current_time:
            raise InvalidStateError("reservation window has expired")

        await cur.execute(
            """
            SELECT
                assignment_id,
                order_id,
                ordered_at
            FROM buyer_orders
            WHERE assignment_id = %s
            FOR UPDATE
            """,
            (purchase_id,),
        )
        existing_order = await cur.fetchone()
        if existing_order is not None:
            if existing_order["order_id"] == decoded.order_id and existing_order["ordered_at"] == decoded.ordered_at:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'order_verified',
                        order_id = %s,
                        order_submitted_at = COALESCE(
                            order_submitted_at,
                            timezone('utc', now())
                        ),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (decoded.order_id, purchase_id),
                )
                return PurchaseOrderSubmitResult(
                    purchase_id=purchase_id,
                    changed=False,
                    status=PurchaseStatus.ORDER_VERIFIED.value,
                    order_id=decoded.order_id,
                    wb_product_id=purchase["wb_product_id"],
                    ordered_at=decoded.ordered_at,
                )
            raise InvalidStateError("assignment already has a different payload")

        await cur.execute(
            """
            SELECT assignment_id
            FROM buyer_orders
            WHERE order_id = %s
            FOR UPDATE
            """,
            (decoded.order_id,),
        )
        duplicate_order = await cur.fetchone()
        if duplicate_order is not None and duplicate_order["assignment_id"] != purchase_id:
            raise DuplicateOrderError("order_id is already linked to another assignment")

        try:
            await cur.execute(
                """
                INSERT INTO buyer_orders (
                    assignment_id,
                    listing_id,
                    buyer_user_id,
                    task_uuid,
                    order_id,
                    wb_product_id,
                    ordered_at,
                    payload_version,
                    raw_payload_json,
                    source
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    purchase_id,
                    purchase["listing_id"],
                    buyer_user_id,
                    purchase["task_uuid"],
                    decoded.order_id,
                    purchase["wb_product_id"],
                    decoded.ordered_at,
                    decoded.payload_version,
                    Json(decoded.raw_payload_json),
                    decoded.source,
                ),
            )
        except UniqueViolation as exc:
            constraint_name = exc.diag.constraint_name if exc.diag is not None else None
            if constraint_name == "uq_buyer_orders_order_id":
                raise DuplicateOrderError("order_id is already linked to another assignment") from exc
            if constraint_name == "uq_buyer_orders_assignment_id":
                raise InvalidStateError("assignment already has submitted order payload") from exc
            raise

        await cur.execute(
            """
            UPDATE assignments
            SET status = 'order_verified',
                order_id = %s,
                order_submitted_at = COALESCE(order_submitted_at, timezone('utc', now())),
                updated_at = timezone('utc', now())
            WHERE id = %s
            """,
            (decoded.order_id, purchase_id),
        )
        await self._notifications.enqueue_assignment_order_verified_for_seller_locked(cur, assignment_id=purchase_id)
        return PurchaseOrderSubmitResult(
            purchase_id=purchase_id,
            changed=True,
            status=PurchaseStatus.ORDER_VERIFIED.value,
            order_id=decoded.order_id,
            wb_product_id=purchase["wb_product_id"],
            ordered_at=decoded.ordered_at,
        )

    async def _load_review_purchase_locked(self, cur, *, purchase_id: int) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                a.id,
                a.listing_id,
                a.buyer_user_id,
                a.status,
                a.task_uuid,
                a.review_phrases,
                l.wb_product_id
            FROM assignments a
            JOIN listings l ON l.id = a.listing_id
            WHERE a.id = %s
            FOR UPDATE OF a, l
            """,
            (purchase_id,),
        )
        return await cur.fetchone()

    async def _load_review_purchase_by_task_uuid_locked(
        self,
        cur,
        *,
        buyer_user_id: int,
        task_uuid: UUID,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                a.id,
                a.listing_id,
                a.buyer_user_id,
                a.status,
                a.task_uuid,
                a.review_phrases,
                l.wb_product_id
            FROM assignments a
            JOIN listings l ON l.id = a.listing_id
            WHERE a.buyer_user_id = %s
              AND a.task_uuid = %s
            FOR UPDATE OF a, l
            """,
            (buyer_user_id, task_uuid),
        )
        return await cur.fetchone()

    async def _store_review_confirmation_locked(
        self,
        cur,
        *,
        purchase: dict[str, Any],
        decoded: DecodedReviewPayload,
        source: str,
        admin_user_id: int | None = None,
        idempotency_seed: str | None = None,
    ) -> PurchaseReviewSubmitResult:
        if purchase["status"] not in {
            PurchaseStatus.PICKED_UP_WAIT_REVIEW.value,
            PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value,
            PurchaseStatus.WITHDRAW_SENT.value,
        }:
            raise InvalidStateError("assignment cannot accept review payload in current state")
        if decoded.task_uuid != purchase["task_uuid"]:
            raise PayloadValidationError("payload field 'task_uuid' does not match assignment")
        if decoded.legacy_wb_product_id is not None and decoded.legacy_wb_product_id != int(purchase["wb_product_id"]):
            raise PayloadValidationError("payload field 'wb_product_id' does not match assignment")

        await cur.execute(
            """
            SELECT
                assignment_id,
                task_uuid,
                reviewed_at,
                rating,
                review_text,
                verification_status,
                verification_reason
            FROM buyer_reviews
            WHERE assignment_id = %s
            FOR UPDATE
            """,
            (purchase["id"],),
        )
        existing_review = await cur.fetchone()
        same_payload = existing_review is not None and (
            existing_review["task_uuid"] == decoded.task_uuid
            and existing_review["reviewed_at"] == decoded.reviewed_at
            and int(existing_review["rating"]) == decoded.rating
            and existing_review["review_text"] == decoded.review_text
        )

        if admin_user_id is not None:
            target_verification_status = _REVIEW_STATUS_VERIFIED_ADMIN
            target_verification_reason = None
        else:
            target_verification_status, target_verification_reason = _evaluate_review_verification(
                rating=decoded.rating,
                review_text=decoded.review_text,
                required_phrases=list(purchase["review_phrases"] or []),
            )

        if same_payload and (
            admin_user_id is None
            or purchase["status"] != PurchaseStatus.PICKED_UP_WAIT_REVIEW.value
            or existing_review["verification_status"] == _REVIEW_STATUS_VERIFIED_ADMIN
        ):
            return PurchaseReviewSubmitResult(
                purchase_id=purchase["id"],
                changed=False,
                status=purchase["status"],
                task_uuid=purchase["task_uuid"],
                wb_product_id=purchase["wb_product_id"],
                reviewed_at=decoded.reviewed_at,
                rating=decoded.rating,
                review_text=decoded.review_text,
                verification_status=existing_review["verification_status"],
                verification_reason=existing_review["verification_reason"],
            )

        if purchase["status"] != PurchaseStatus.PICKED_UP_WAIT_REVIEW.value:
            raise InvalidStateError("assignment review is already completed")

        verified_at = datetime.now(tz=UTC) if target_verification_status != _REVIEW_STATUS_PENDING_MANUAL else None
        if existing_review is None:
            try:
                await cur.execute(
                    """
                    INSERT INTO buyer_reviews (
                        assignment_id,
                        listing_id,
                        buyer_user_id,
                        task_uuid,
                        wb_product_id,
                        reviewed_at,
                        rating,
                        review_text,
                        verification_status,
                        verification_reason,
                        verified_at,
                        verified_by_admin_user_id,
                        payload_version,
                        raw_payload_json,
                        source
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        purchase["id"],
                        purchase["listing_id"],
                        purchase["buyer_user_id"],
                        purchase["task_uuid"],
                        purchase["wb_product_id"],
                        decoded.reviewed_at,
                        decoded.rating,
                        decoded.review_text,
                        target_verification_status,
                        target_verification_reason,
                        verified_at,
                        admin_user_id,
                        decoded.payload_version,
                        Json(decoded.raw_payload_json),
                        source,
                    ),
                )
            except UniqueViolation as exc:
                constraint_name = exc.diag.constraint_name if exc.diag is not None else None
                if constraint_name == "uq_buyer_reviews_assignment_id":
                    raise InvalidStateError("assignment already has submitted review payload") from exc
                raise
        else:
            await cur.execute(
                """
                UPDATE buyer_reviews
                SET task_uuid = %s,
                    reviewed_at = %s,
                    rating = %s,
                    review_text = %s,
                    verification_status = %s,
                    verification_reason = %s,
                    verified_at = %s,
                    verified_by_admin_user_id = %s,
                    payload_version = %s,
                    raw_payload_json = %s,
                    source = %s,
                    updated_at = timezone('utc', now())
                WHERE assignment_id = %s
                """,
                (
                    purchase["task_uuid"],
                    decoded.reviewed_at,
                    decoded.rating,
                    decoded.review_text,
                    target_verification_status,
                    target_verification_reason,
                    verified_at,
                    admin_user_id,
                    decoded.payload_version,
                    Json(decoded.raw_payload_json),
                    source,
                    purchase["id"],
                ),
            )

        purchase_status = purchase["status"]
        if target_verification_status != _REVIEW_STATUS_PENDING_MANUAL:
            await cur.execute(
                """
                UPDATE assignments
                SET status = 'picked_up_wait_unlock',
                    updated_at = timezone('utc', now())
                WHERE id = %s
                """,
                (purchase["id"],),
            )
            purchase_status = PurchaseStatus.PICKED_UP_WAIT_UNLOCK.value
            if purchase["status"] == PurchaseStatus.PICKED_UP_WAIT_REVIEW.value:
                await self._notifications.enqueue_assignment_review_confirmed_for_seller_locked(
                    cur,
                    assignment_id=purchase["id"],
                )
            if admin_user_id is not None and idempotency_seed is not None:
                await self._finance.insert_admin_audit_locked(
                    cur,
                    admin_user_id=admin_user_id,
                    action="assignment_review_verified_admin",
                    target_type="assignment",
                    target_id=str(purchase["id"]),
                    payload={
                        "assignment_id": purchase["id"],
                        "task_uuid": str(purchase["task_uuid"]),
                        "wb_product_id": purchase["wb_product_id"],
                        "reviewed_at": decoded.reviewed_at.isoformat(),
                        "rating": decoded.rating,
                        "review_text": decoded.review_text,
                    },
                    idempotency_key=f"{idempotency_seed}:audit",
                )

        return PurchaseReviewSubmitResult(
            purchase_id=purchase["id"],
            changed=True,
            status=purchase_status,
            task_uuid=purchase["task_uuid"],
            wb_product_id=purchase["wb_product_id"],
            reviewed_at=decoded.reviewed_at,
            rating=decoded.rating,
            review_text=decoded.review_text,
            verification_status=target_verification_status,
            verification_reason=target_verification_reason,
        )

    async def _cancel_purchase_to_status_locked(
        self,
        cur,
        *,
        purchase_id: int,
        new_status: PurchaseStatus,
        idempotency_seed: str,
        notification_event: str | None,
    ) -> StatusChangeResult:
        if new_status.value not in _CANCELLATION_STATES:
            raise ValueError(f"new_status must be one of {_CANCELLATION_STATES}")

        await cur.execute(
            """
            SELECT
                a.id,
                a.listing_id,
                a.reward_usdt,
                a.status,
                l.seller_user_id
            FROM assignments a
            JOIN listings l ON l.id = a.listing_id
            WHERE a.id = %s
            FOR UPDATE OF a, l
            """,
            (purchase_id,),
        )
        purchase = await cur.fetchone()
        if purchase is None:
            raise NotFoundError(f"assignment {purchase_id} not found")
        if purchase["status"] == new_status.value:
            return StatusChangeResult(changed=False)
        if purchase["status"] not in _OPEN_PURCHASE_STATES:
            raise InvalidStateError("assignment cannot be cancelled from current state")

        seller_collateral_account_id = await self._ensure_owner_account(
            cur,
            owner_user_id=int(purchase["seller_user_id"]),
            account_kind="seller_collateral",
        )
        reward_reserved_account_id = await self._ensure_system_account(cur, account_kind="reward_reserved")

        await cur.execute(
            """
            UPDATE assignments
            SET status = %s,
                cancel_reason = %s,
                updated_at = timezone('utc', now())
            WHERE id = %s
            """,
            (new_status.value, new_status.value, purchase_id),
        )

        await cur.execute(
            """
            UPDATE listings
            SET available_slots = LEAST(slot_count, available_slots + 1),
                updated_at = timezone('utc', now())
            WHERE id = %s
            """,
            (purchase["listing_id"],),
        )

        await self._transfer_locked(
            cur,
            from_account_id=reward_reserved_account_id,
            to_account_id=seller_collateral_account_id,
            amount_usdt=_normalize_amount(purchase["reward_usdt"]),
            event_type="slot_release",
            idempotency_key=_ledger_key(idempotency_seed),
            entity_type="assignment",
            entity_id=purchase_id,
            metadata={"assignment_id": purchase_id, "status": new_status.value},
        )

        await cur.execute(
            """
            UPDATE balance_holds
            SET status = 'released',
                released_at = timezone('utc', now())
            WHERE assignment_id = %s
                AND hold_type = 'slot_reserve'
                AND status = 'active'
            """,
            (purchase_id,),
        )

        if notification_event == "reservation_expired":
            await self._notifications.enqueue_assignment_reservation_expired_for_buyer_locked(
                cur,
                assignment_id=purchase_id,
            )
        elif notification_event == "assignment_returned":
            await self._notifications.enqueue_assignment_returned_locked(cur, assignment_id=purchase_id)
        elif notification_event == "delivery_expired":
            await self._notifications.enqueue_assignment_delivery_expired_locked(cur, assignment_id=purchase_id)

        return StatusChangeResult(changed=True)

    async def _find_reservation_by_idempotency_locked(
        self,
        cur,
        *,
        idempotency_key: str,
    ) -> PurchaseReservationResult | None:
        await cur.execute(
            """
            SELECT id, reward_usdt, reservation_expires_at, task_uuid
            FROM assignments
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return PurchaseReservationResult(
            purchase_id=row["id"],
            created=False,
            cashback_usdt=row["reward_usdt"],
            reservation_expires_at=row["reservation_expires_at"],
            task_uuid=row["task_uuid"],
        )

    async def _ensure_buyer_user_exists_locked(self, cur, *, buyer_user_id: int) -> None:
        await cur.execute(
            """
            SELECT id
            FROM users
            WHERE id = %s
              AND (
                    is_buyer
                    OR is_admin
                    OR role IN ('buyer', 'admin')
              )
            """,
            (buyer_user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"buyer user {buyer_user_id} not found")

    async def _ensure_buyer_has_not_purchased_item_locked(
        self,
        cur,
        *,
        buyer_user_id: int,
        wb_product_id: int,
    ) -> None:
        await cur.execute(
            """
            SELECT 1
            FROM buyer_orders
            WHERE buyer_user_id = %s
              AND wb_product_id = %s
            LIMIT 1
            """,
            (buyer_user_id, wb_product_id),
        )
        if await cur.fetchone() is not None:
            raise InvalidStateError("buyer already purchased this item")

        await cur.execute(
            """
            SELECT 1
            FROM assignments a
            JOIN listings lx ON lx.id = a.listing_id
            WHERE a.buyer_user_id = %s
              AND lx.wb_product_id = %s
              AND a.status = ANY(%s)
            LIMIT 1
            """,
            (buyer_user_id, wb_product_id, list(_ACTIVE_PURCHASE_STATES)),
        )
        if await cur.fetchone() is not None:
            raise InvalidStateError("buyer already has assignment for this item")

    async def _ensure_owner_account(self, cur, *, owner_user_id: int, account_kind: str) -> int:
        return await self._finance.ensure_owner_account_locked(
            cur,
            owner_user_id=owner_user_id,
            account_kind=account_kind,
        )

    async def _ensure_system_account(self, cur, *, account_kind: str) -> int:
        return await self._finance.ensure_system_account_locked(cur, account_kind=account_kind)

    async def _transfer_locked(
        self,
        cur,
        *,
        from_account_id: int,
        to_account_id: int,
        amount_usdt: Decimal,
        event_type: str,
        idempotency_key: str,
        entity_type: str | None,
        entity_id: int | None,
        metadata: dict[str, Any],
    ) -> Any:
        return await self._finance.transfer_locked(
            cur,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_usdt=amount_usdt,
            event_type=event_type,
            idempotency_key=idempotency_key,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata,
        )

    async def _upsert_hold(
        self,
        cur,
        *,
        account_id: int,
        hold_type: str,
        status: str,
        amount_usdt: Decimal,
        idempotency_key: str,
        listing_id: int | None = None,
        assignment_id: int | None = None,
        withdrawal_request_id: int | None = None,
    ) -> int:
        return await self._finance.upsert_hold_locked(
            cur,
            account_id=account_id,
            hold_type=hold_type,
            status=status,
            amount_usdt=amount_usdt,
            idempotency_key=idempotency_key,
            listing_id=listing_id,
            assignment_id=assignment_id,
            withdrawal_request_id=withdrawal_request_id,
        )

    @staticmethod
    def _pick_purchase_review_phrases(review_phrases: list[str]) -> list[str]:
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


def _evaluate_review_verification(
    *,
    rating: int,
    review_text: str,
    required_phrases: list[str],
) -> tuple[str, str | None]:
    missing_phrases = _missing_required_review_phrases(
        review_text=review_text,
        required_phrases=required_phrases,
    )
    if rating == 5 and not missing_phrases:
        return _REVIEW_STATUS_VERIFIED_AUTO, None

    reasons: list[str] = []
    if rating != 5:
        reasons.append("Нужна оценка 5 из 5.")
    if missing_phrases:
        reasons.append("В тексте не хватает обязательных фраз: " + "; ".join(missing_phrases) + ".")
    if not reasons:
        reasons.append("Автоматическая проверка не пройдена.")
    return _REVIEW_STATUS_PENDING_MANUAL, " ".join(reasons)


def _missing_required_review_phrases(
    *,
    review_text: str,
    required_phrases: list[str],
) -> list[str]:
    normalized_review_text = _normalize_review_match_text(review_text)
    missing: list[str] = []
    for phrase in required_phrases:
        normalized_phrase = _normalize_review_match_text(phrase)
        if normalized_phrase and normalized_phrase not in normalized_review_text:
            missing.append(phrase.strip())
    return missing


def _normalize_review_match_text(value: str) -> str:
    return _REVIEW_NORMALIZE_WHITESPACE_RE.sub(" ", value).strip().casefold()


def _ledger_key(idempotency_key: str) -> str:
    return f"ledger:{idempotency_key}"


def _hold_key(idempotency_key: str) -> str:
    return f"hold:{idempotency_key}"


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
