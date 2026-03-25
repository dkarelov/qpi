from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.errors import (
    InsufficientFundsError,
    InvalidStateError,
    NoSlotsAvailableError,
    NotFoundError,
)
from libs.domain.models import (
    ActiveWithdrawalRequestView,
    AssignmentReservationResult,
    BuyerBalanceSnapshot,
    ManualDepositResult,
    PendingWithdrawalView,
    ProcessedWithdrawalView,
    StatusChangeResult,
    TransferResult,
    WithdrawalHistoryItem,
    WithdrawalRequestDetail,
    WithdrawalRequestResult,
)
from libs.domain.notifications import (
    EVENT_WITHDRAW_REJECTED_REQUESTER,
    EVENT_WITHDRAW_SENT_REQUESTER,
    NotificationService,
)

_CANCELLATION_STATES = {
    "expired_2h",
    "buyer_cancelled",
    "wb_invalid",
    "returned_within_14d",
    "delivery_expired",
}
_WITHDRAWAL_REQUESTER_ROLES = frozenset({"buyer", "seller"})


class FinanceService:
    """Transactional money and assignment primitives using plain SQL."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._notifications = NotificationService(pool)

    async def ensure_admin_user(self, *, telegram_id: int, username: str | None) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE telegram_id = %s
                    FOR UPDATE
                    """,
                    (telegram_id,),
                )
                existing = await cur.fetchone()
                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO users (
                            telegram_id,
                            username,
                            role,
                            is_seller,
                            is_buyer,
                            is_admin
                        )
                        VALUES (%s, %s, 'admin', false, false, true)
                        RETURNING id
                        """,
                        (telegram_id, username),
                    )
                    created = await cur.fetchone()
                    return int(created["id"])
                await cur.execute(
                    """
                    UPDATE users
                    SET username = COALESCE(%s, username),
                        is_admin = true,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (username, existing["id"]),
                )
                return int(existing["id"])

        return await run_in_transaction(self._pool, operation)

    async def ensure_system_account_id(self, *, account_kind: str) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self.ensure_system_account_locked(cur, account_kind=account_kind)

        return await run_in_transaction(self._pool, operation)

    async def resolve_manual_deposit_target(
        self,
        *,
        target_telegram_id: int,
        account_kind: str,
    ) -> tuple[int, int]:
        required_role_by_account_kind = {
            "seller_available": "seller",
            "buyer_available": "buyer",
        }
        required_role = required_role_by_account_kind.get(account_kind)
        if required_role is None:
            raise ValueError("account_kind must be seller|buyer")

        async def operation(conn: AsyncConnection) -> tuple[int, int]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, role, is_seller, is_buyer, is_admin
                    FROM users
                    WHERE telegram_id = %s
                    FOR UPDATE
                    """,
                    (target_telegram_id,),
                )
                user_row = await cur.fetchone()
                if user_row is None:
                    raise NotFoundError(f"user with telegram_id {target_telegram_id} not found")

                has_required_role = False
                if required_role == "seller":
                    has_required_role = bool(
                        user_row["is_seller"]
                        or user_row["is_admin"]
                        or user_row["role"] in {"seller", "admin"}
                    )
                elif required_role == "buyer":
                    has_required_role = bool(
                        user_row["is_buyer"]
                        or user_row["is_admin"]
                        or user_row["role"] in {"buyer", "admin"}
                    )
                if not has_required_role:
                    raise InvalidStateError(
                        f"user capabilities are incompatible with {account_kind}"
                    )

                account_id = await self.ensure_owner_account_locked(
                    cur,
                    owner_user_id=int(user_row["id"]),
                    account_kind=account_kind,
                )
                return int(user_row["id"]), account_id

        return await run_in_transaction(self._pool, operation)

    async def ensure_owner_account_locked(
        self,
        cur,
        *,
        owner_user_id: int,
        account_kind: str,
    ) -> int:
        return await self._ensure_system_or_owner_account_locked(
            cur,
            owner_user_id=owner_user_id,
            account_kind=account_kind,
        )

    async def ensure_system_account_locked(self, cur, *, account_kind: str) -> int:
        return await self._ensure_system_or_owner_account_locked(
            cur,
            owner_user_id=None,
            account_kind=account_kind,
        )

    async def transfer_locked(
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
    ) -> TransferResult:
        return await self._transfer_locked(
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

    async def upsert_hold_locked(
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
        return await self._upsert_hold(
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

    async def insert_admin_audit_locked(
        self,
        cur,
        *,
        admin_user_id: int,
        action: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        await self._insert_admin_audit(
            cur,
            admin_user_id=admin_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    async def provision_system_balance_locked(
        self,
        cur,
        *,
        account_id: int,
        amount_usdt: Decimal,
        event_type: str,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> int:
        normalized_amount = _normalize_amount(amount_usdt)
        await cur.execute(
            """
            SELECT id
            FROM system_balance_provisions
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            return int(existing["id"])

        await cur.execute(
            """
            UPDATE accounts
            SET current_balance_usdt = current_balance_usdt + %s,
                updated_at = timezone('utc', now())
            WHERE id = %s
            RETURNING id
            """,
            (normalized_amount, account_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"system account {account_id} not found")

        await cur.execute(
            """
            INSERT INTO system_balance_provisions (
                account_id,
                amount_usdt,
                event_type,
                metadata_json,
                idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                account_id,
                normalized_amount,
                event_type,
                Json(metadata),
                idempotency_key,
            ),
        )
        created = await cur.fetchone()
        return int(created["id"])

    async def lock_listing_collateral(
        self,
        *,
        listing_id: int,
        seller_available_account_id: int,
        seller_collateral_account_id: int,
        amount_usdt: Decimal,
        idempotency_key: str,
    ) -> TransferResult:
        amount = _normalize_amount(amount_usdt)

        async def operation(conn: AsyncConnection) -> TransferResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                result = await self._transfer_locked(
                    cur,
                    from_account_id=seller_available_account_id,
                    to_account_id=seller_collateral_account_id,
                    amount_usdt=amount,
                    event_type="collateral_lock",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="listing",
                    entity_id=listing_id,
                    metadata={"listing_id": listing_id},
                )
                await self._upsert_hold(
                    cur,
                    account_id=seller_collateral_account_id,
                    hold_type="collateral",
                    status="active",
                    amount_usdt=amount,
                    listing_id=listing_id,
                    idempotency_key=_hold_key(idempotency_key),
                )
                return result

        return await run_in_transaction(self._pool, operation)

    async def create_assignment_reservation(
        self,
        *,
        listing_id: int,
        buyer_user_id: int,
        seller_collateral_account_id: int,
        reward_reserved_account_id: int,
        idempotency_key: str,
        reservation_timeout_hours: int = 4,
    ) -> AssignmentReservationResult:
        if reservation_timeout_hours < 1:
            raise ValueError("reservation_timeout_hours must be >= 1")

        async def operation(conn: AsyncConnection) -> AssignmentReservationResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, reward_usdt, reservation_expires_at
                    FROM assignments
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    return AssignmentReservationResult(
                        assignment_id=existing["id"],
                        created=False,
                        reward_usdt=existing["reward_usdt"],
                        reservation_expires_at=existing["reservation_expires_at"],
                    )

                await cur.execute(
                    """
                    SELECT id, status, reward_usdt, wb_product_id, available_slots, deleted_at
                    FROM listings
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (listing_id,),
                )
                listing = await cur.fetchone()
                if listing is None:
                    raise NotFoundError(f"listing {listing_id} not found")

                if listing["deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")

                if listing["status"] != "active":
                    raise InvalidStateError("listing must be active for reservation")

                if listing["available_slots"] <= 0:
                    raise NoSlotsAvailableError("listing has no available slots")

                await cur.execute(
                    """
                    UPDATE listings
                    SET available_slots = available_slots - 1,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (listing_id,),
                )

                reward_usdt = _normalize_amount(listing["reward_usdt"])

                try:
                    await cur.execute(
                        """
                        INSERT INTO assignments (
                            listing_id,
                            buyer_user_id,
                            wb_product_id,
                            status,
                            reward_usdt,
                            reservation_expires_at,
                            idempotency_key
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            'reserved',
                            %s,
                            timezone('utc', now()) + (%s * interval '1 hour'),
                            %s
                        )
                        RETURNING id, reservation_expires_at
                        """,
                        (
                            listing_id,
                            buyer_user_id,
                            listing["wb_product_id"],
                            reward_usdt,
                            reservation_timeout_hours,
                            idempotency_key,
                        ),
                    )
                except UniqueViolation as exc:
                    constraint_name = exc.diag.constraint_name if exc.diag is not None else None
                    if constraint_name == "uq_assignments_buyer_product_active":
                        raise InvalidStateError(
                            "buyer already has assignment for this item"
                        ) from exc
                    raise
                assignment = await cur.fetchone()

                await self._transfer_locked(
                    cur,
                    from_account_id=seller_collateral_account_id,
                    to_account_id=reward_reserved_account_id,
                    amount_usdt=reward_usdt,
                    event_type="slot_reserve",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="assignment",
                    entity_id=assignment["id"],
                    metadata={"assignment_id": assignment["id"], "listing_id": listing_id},
                )

                await self._upsert_hold(
                    cur,
                    account_id=reward_reserved_account_id,
                    hold_type="slot_reserve",
                    status="active",
                    amount_usdt=reward_usdt,
                    listing_id=listing_id,
                    assignment_id=assignment["id"],
                    idempotency_key=_hold_key(idempotency_key),
                )

                return AssignmentReservationResult(
                    assignment_id=assignment["id"],
                    created=True,
                    reward_usdt=reward_usdt,
                    reservation_expires_at=assignment["reservation_expires_at"],
                )

        return await run_in_transaction(self._pool, operation)

    async def cancel_assignment_reservation(
        self,
        *,
        assignment_id: int,
        new_status: str,
        seller_collateral_account_id: int,
        reward_reserved_account_id: int,
        idempotency_key: str,
        notification_event: str | None = None,
    ) -> StatusChangeResult:
        if new_status not in _CANCELLATION_STATES:
            raise ValueError(f"new_status must be one of {_CANCELLATION_STATES}")

        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, listing_id, reward_usdt, status
                    FROM assignments
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (assignment_id,),
                )
                assignment = await cur.fetchone()
                if assignment is None:
                    raise NotFoundError(f"assignment {assignment_id} not found")

                if assignment["status"] == new_status:
                    return StatusChangeResult(changed=False)

                if assignment["status"] not in {
                    "reserved",
                    "order_verified",
                    "picked_up_wait_unlock",
                }:
                    raise InvalidStateError("assignment cannot be cancelled from current state")

                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = %s,
                        cancel_reason = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (new_status, new_status, assignment_id),
                )

                await cur.execute(
                    """
                    UPDATE listings
                    SET available_slots = LEAST(slot_count, available_slots + 1),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (assignment["listing_id"],),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=reward_reserved_account_id,
                    to_account_id=seller_collateral_account_id,
                    amount_usdt=_normalize_amount(assignment["reward_usdt"]),
                    event_type="slot_release",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="assignment",
                    entity_id=assignment_id,
                    metadata={"assignment_id": assignment_id, "status": new_status},
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
                    (assignment_id,),
                )

                if notification_event == "reservation_expired":
                    await (
                        self._notifications.enqueue_assignment_reservation_expired_for_buyer_locked(
                            cur,
                            assignment_id=assignment_id,
                        )
                    )
                elif notification_event == "assignment_returned":
                    await self._notifications.enqueue_assignment_returned_locked(
                        cur,
                        assignment_id=assignment_id,
                    )
                elif notification_event == "delivery_expired":
                    await self._notifications.enqueue_assignment_delivery_expired_locked(
                        cur,
                        assignment_id=assignment_id,
                    )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def unlock_assignment_reward(
        self,
        *,
        assignment_id: int,
        buyer_available_account_id: int,
        reward_reserved_account_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, reward_usdt, status, unlock_at
                    FROM assignments
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (assignment_id,),
                )
                assignment = await cur.fetchone()
                if assignment is None:
                    raise NotFoundError(f"assignment {assignment_id} not found")

                if assignment["status"] == "withdraw_sent":
                    return StatusChangeResult(changed=False)

                if assignment["status"] != "picked_up_wait_unlock":
                    raise InvalidStateError("assignment must be in picked_up_wait_unlock state")

                if assignment["unlock_at"] is None:
                    raise InvalidStateError("assignment unlock_at is not set")

                await cur.execute("SELECT now() AS current_time")
                now_row = await cur.fetchone()
                if assignment["unlock_at"] > now_row["current_time"]:
                    raise InvalidStateError("assignment unlock time has not passed yet")

                await cur.execute(
                    """
                    UPDATE assignments
                    SET status = 'withdraw_sent',
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=reward_reserved_account_id,
                    to_account_id=buyer_available_account_id,
                    amount_usdt=_normalize_amount(assignment["reward_usdt"]),
                    event_type="reward_unlock",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="assignment",
                    entity_id=assignment_id,
                    metadata={"assignment_id": assignment_id},
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
                    (assignment_id,),
                )

                await self._notifications.enqueue_assignment_reward_unlocked_locked(
                    cur,
                    assignment_id=assignment_id,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def create_withdrawal_request(
        self,
        *,
        requester_user_id: int,
        requester_role: str,
        from_account_id: int,
        pending_account_id: int,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> WithdrawalRequestResult:
        normalized_requester_role = _normalize_requester_role(requester_role)
        amount = _normalize_amount(amount_usdt)

        async def operation(conn: AsyncConnection) -> WithdrawalRequestResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM withdrawal_requests
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    return WithdrawalRequestResult(
                        withdrawal_request_id=existing["id"],
                        created=False,
                    )

                await cur.execute(
                    """
                    SELECT id
                    FROM withdrawal_requests
                    WHERE requester_user_id = %s
                      AND requester_role = %s
                      AND status = 'withdraw_pending_admin'
                    FOR UPDATE
                    """,
                    (requester_user_id, normalized_requester_role),
                )
                active = await cur.fetchone()
                if active is not None:
                    raise InvalidStateError(
                        f"{normalized_requester_role} already has active withdrawal request"
                    )

                await self._transfer_locked(
                    cur,
                    from_account_id=from_account_id,
                    to_account_id=pending_account_id,
                    amount_usdt=amount,
                    event_type="withdraw_request",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="withdrawal_request",
                    entity_id=None,
                    metadata={
                        "requester_user_id": requester_user_id,
                        "requester_role": normalized_requester_role,
                    },
                )

                try:
                    await cur.execute(
                        """
                        INSERT INTO withdrawal_requests (
                            requester_user_id,
                            requester_role,
                            from_account_id,
                            to_account_id,
                            amount_usdt,
                            status,
                            payout_address,
                            idempotency_key
                        )
                        VALUES (%s, %s, %s, %s, %s, 'withdraw_pending_admin', %s, %s)
                        RETURNING id
                        """,
                        (
                            requester_user_id,
                            normalized_requester_role,
                            from_account_id,
                            pending_account_id,
                            amount,
                            payout_address,
                            idempotency_key,
                        ),
                    )
                except UniqueViolation as exc:
                    constraint_name = exc.diag.constraint_name if exc.diag is not None else None
                    if constraint_name == "uq_withdrawal_requests_requester_active":
                        raise InvalidStateError(
                            f"{normalized_requester_role} already has active withdrawal request"
                        ) from exc
                    raise
                withdrawal_request = await cur.fetchone()

                await self._upsert_hold(
                    cur,
                    account_id=pending_account_id,
                    hold_type="withdrawal",
                    status="active",
                    amount_usdt=amount,
                    withdrawal_request_id=withdrawal_request["id"],
                    idempotency_key=_hold_key(idempotency_key),
                )

                await self._notifications.enqueue_withdraw_created_for_admins_locked(
                    cur,
                    request_id=withdrawal_request["id"],
                )

                return WithdrawalRequestResult(
                    withdrawal_request_id=withdrawal_request["id"],
                    created=True,
                )

        return await run_in_transaction(self._pool, operation)

    async def cancel_withdrawal_request(
        self,
        *,
        request_id: int,
        requester_user_id: int,
        requester_role: str,
        idempotency_key: str,
    ) -> StatusChangeResult:
        normalized_requester_role = _normalize_requester_role(requester_role)

        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        requester_user_id,
                        requester_role,
                        from_account_id,
                        to_account_id,
                        status,
                        amount_usdt
                    FROM withdrawal_requests
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (request_id,),
                )
                request = await cur.fetchone()
                if request is None:
                    raise NotFoundError(f"withdrawal request {request_id} not found")

                if (
                    request["requester_user_id"] != requester_user_id
                    or request["requester_role"] != normalized_requester_role
                ):
                    raise InvalidStateError("withdrawal request does not belong to requester")

                if request["status"] == "cancelled":
                    return StatusChangeResult(changed=False)

                if request["status"] != "withdraw_pending_admin":
                    raise InvalidStateError(
                        "withdrawal request cannot be cancelled from current state"
                    )

                await cur.execute(
                    """
                    UPDATE withdrawal_requests
                    SET status = 'cancelled',
                        processed_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (request_id,),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=request["to_account_id"],
                    to_account_id=request["from_account_id"],
                    amount_usdt=_normalize_amount(request["amount_usdt"]),
                    event_type="withdraw_cancel",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="withdrawal_request",
                    entity_id=request_id,
                    metadata={
                        "request_id": request_id,
                        "requester_user_id": requester_user_id,
                        "requester_role": normalized_requester_role,
                    },
                )

                await cur.execute(
                    """
                    UPDATE balance_holds
                    SET status = 'released',
                        released_at = timezone('utc', now())
                    WHERE withdrawal_request_id = %s
                        AND hold_type = 'withdrawal'
                        AND status = 'active'
                    """,
                    (request_id,),
                )

                await self._notifications.enqueue_withdraw_cancelled_for_admins_locked(
                    cur,
                    request_id=request_id,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def reject_withdrawal_request(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        reason: str | None = None,
        idempotency_key: str,
    ) -> StatusChangeResult:
        normalized_reason = reason.strip() if reason is not None else ""

        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status, amount_usdt, from_account_id, to_account_id
                    FROM withdrawal_requests
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (request_id,),
                )
                request = await cur.fetchone()
                if request is None:
                    raise NotFoundError(f"withdrawal request {request_id} not found")

                if request["status"] == "rejected":
                    return StatusChangeResult(changed=False)

                if request["status"] != "withdraw_pending_admin":
                    raise InvalidStateError(
                        "withdrawal request cannot be rejected from current state"
                    )

                await cur.execute(
                    """
                    UPDATE withdrawal_requests
                    SET status = 'rejected',
                        admin_user_id = %s,
                        processed_at = timezone('utc', now()),
                        note = CASE WHEN %s <> '' THEN %s ELSE note END
                    WHERE id = %s
                    """,
                    (admin_user_id, normalized_reason, normalized_reason, request_id),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=request["to_account_id"],
                    to_account_id=request["from_account_id"],
                    amount_usdt=_normalize_amount(request["amount_usdt"]),
                    event_type="withdraw_reject",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="withdrawal_request",
                    entity_id=request_id,
                    metadata={"request_id": request_id},
                )

                await cur.execute(
                    """
                    UPDATE balance_holds
                    SET status = 'released',
                        released_at = timezone('utc', now())
                    WHERE withdrawal_request_id = %s
                        AND hold_type = 'withdrawal'
                        AND status = 'active'
                    """,
                    (request_id,),
                )

                await self._insert_admin_audit(
                    cur,
                    admin_user_id=admin_user_id,
                    action="withdraw_rejected",
                    target_type="withdrawal_request",
                    target_id=str(request_id),
                    payload={"request_id": request_id, "reason": normalized_reason or None},
                    idempotency_key=idempotency_key,
                )

                await self._notifications.enqueue_withdraw_status_for_requester_locked(
                    cur,
                    request_id=request_id,
                    event_type=EVENT_WITHDRAW_REJECTED_REQUESTER,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def complete_withdrawal_request(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        system_payout_account_id: int,
        tx_hash: str,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status, amount_usdt, to_account_id
                    FROM withdrawal_requests
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (request_id,),
                )
                request = await cur.fetchone()
                if request is None:
                    raise NotFoundError(f"withdrawal request {request_id} not found")

                if request["status"] == "withdraw_sent":
                    return StatusChangeResult(changed=False)

                if request["status"] != "withdraw_pending_admin":
                    raise InvalidStateError("withdrawal request must be pending admin")

                await cur.execute(
                    """
                    UPDATE withdrawal_requests
                    SET status = 'withdraw_sent',
                        admin_user_id = %s,
                        processed_at = timezone('utc', now()),
                        sent_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (admin_user_id, request_id),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=request["to_account_id"],
                    to_account_id=system_payout_account_id,
                    amount_usdt=_normalize_amount(request["amount_usdt"]),
                    event_type="withdraw_sent",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="withdrawal_request",
                    entity_id=request_id,
                    metadata={"request_id": request_id, "tx_hash": tx_hash},
                )

                await cur.execute(
                    """
                    INSERT INTO payouts (
                        withdrawal_request_id,
                        tx_hash,
                        status
                    )
                    VALUES (%s, %s, 'sent')
                    ON CONFLICT (withdrawal_request_id)
                    DO UPDATE SET
                        tx_hash = EXCLUDED.tx_hash,
                        status = EXCLUDED.status,
                        updated_at = timezone('utc', now())
                    """,
                    (request_id, tx_hash),
                )

                await cur.execute(
                    """
                    UPDATE balance_holds
                    SET status = 'consumed',
                        released_at = timezone('utc', now())
                    WHERE withdrawal_request_id = %s
                        AND hold_type = 'withdrawal'
                        AND status = 'active'
                    """,
                    (request_id,),
                )

                await self._insert_admin_audit(
                    cur,
                    admin_user_id=admin_user_id,
                    action="withdraw_sent",
                    target_type="withdrawal_request",
                    target_id=str(request_id),
                    payload={"request_id": request_id, "tx_hash": tx_hash},
                    idempotency_key=idempotency_key,
                )

                await self._notifications.enqueue_withdraw_status_for_requester_locked(
                    cur,
                    request_id=request_id,
                    event_type=EVENT_WITHDRAW_SENT_REQUESTER,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def mark_withdrawal_sent(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        system_payout_account_id: int,
        tx_hash: str,
        idempotency_key: str,
    ) -> StatusChangeResult:
        return await self.complete_withdrawal_request(
            request_id=request_id,
            admin_user_id=admin_user_id,
            system_payout_account_id=system_payout_account_id,
            tx_hash=tx_hash,
            idempotency_key=idempotency_key,
        )

    async def manual_deposit_credit(
        self,
        *,
        admin_user_id: int,
        target_user_id: int,
        target_account_id: int,
        amount_usdt: Decimal,
        external_reference: str,
        idempotency_key: str,
        tx_hash: str | None = None,
        note: str | None = None,
    ) -> ManualDepositResult:
        normalized_reference = external_reference.strip()
        if not normalized_reference:
            raise ValueError("external_reference must not be empty")
        normalized_tx_hash = tx_hash.strip() if tx_hash is not None else None
        normalized_note = note.strip() if note is not None else None
        amount = _normalize_amount(amount_usdt)

        async def operation(conn: AsyncConnection) -> ManualDepositResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, ledger_entry_id
                    FROM manual_deposits
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    return ManualDepositResult(
                        manual_deposit_id=existing["id"],
                        ledger_entry_id=existing["ledger_entry_id"],
                        created=False,
                    )

                await cur.execute(
                    """
                    SELECT id, owner_user_id, account_kind
                    FROM accounts
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (target_account_id,),
                )
                target_account = await cur.fetchone()
                if target_account is None:
                    raise NotFoundError(f"target account {target_account_id} not found")
                if target_account["owner_user_id"] != target_user_id:
                    raise InvalidStateError("target account does not belong to target user")

                system_payout_account_id = await self.ensure_system_account_locked(
                    cur,
                    account_kind="system_payout",
                )
                await self.provision_system_balance_locked(
                    cur,
                    account_id=system_payout_account_id,
                    amount_usdt=amount,
                    event_type="manual_deposit_credit",
                    idempotency_key=f"{idempotency_key}:provision",
                    metadata={
                        "target_user_id": target_user_id,
                        "target_account_id": target_account_id,
                        "external_reference": normalized_reference,
                        "tx_hash": normalized_tx_hash,
                        "note": normalized_note,
                    },
                )

                transfer_result = await self._transfer_locked(
                    cur,
                    from_account_id=system_payout_account_id,
                    to_account_id=target_account_id,
                    amount_usdt=amount,
                    event_type="manual_deposit_credit",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="manual_deposit",
                    entity_id=None,
                    metadata={
                        "target_user_id": target_user_id,
                        "target_account_id": target_account_id,
                        "external_reference": normalized_reference,
                        "tx_hash": normalized_tx_hash,
                        "note": normalized_note,
                    },
                )

                await cur.execute(
                    """
                    INSERT INTO manual_deposits (
                        target_user_id,
                        target_account_id,
                        admin_user_id,
                        amount_usdt,
                        external_reference,
                        tx_hash,
                        note,
                        ledger_entry_id,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        target_user_id,
                        target_account_id,
                        admin_user_id,
                        amount,
                        normalized_reference,
                        normalized_tx_hash,
                        normalized_note,
                        transfer_result.entry_id,
                        idempotency_key,
                    ),
                )
                created = await cur.fetchone()

                await self._insert_admin_audit(
                    cur,
                    admin_user_id=admin_user_id,
                    action="manual_deposit_credit",
                    target_type="user_account",
                    target_id=str(target_account_id),
                    payload={
                        "manual_deposit_id": created["id"],
                        "target_user_id": target_user_id,
                        "target_account_id": target_account_id,
                        "amount_usdt": str(amount),
                        "external_reference": normalized_reference,
                        "tx_hash": normalized_tx_hash,
                        "note": normalized_note,
                    },
                    idempotency_key=f"{idempotency_key}:audit",
                )

                await self._notifications.enqueue_manual_balance_credit_locked(
                    cur,
                    target_user_id=target_user_id,
                    amount_usdt=amount,
                    recipient_role=(
                        "seller"
                        if target_account["account_kind"] == "seller_available"
                        else "buyer"
                    ),
                    dedupe_key=f"manual_deposit:{created['id']}:target:{target_user_id}",
                )

                return ManualDepositResult(
                    manual_deposit_id=created["id"],
                    ledger_entry_id=transfer_result.entry_id,
                    created=True,
                )

        return await run_in_transaction(self._pool, operation)

    async def get_buyer_balance_snapshot(
        self,
        *,
        buyer_user_id: int,
    ) -> BuyerBalanceSnapshot:
        async def operation(conn: AsyncConnection) -> BuyerBalanceSnapshot:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        COALESCE(
                            MAX(
                                CASE
                                    WHEN account_kind = 'buyer_available'
                                    THEN current_balance_usdt
                                END
                            ),
                            0
                        ) AS buyer_available_usdt,
                        COALESCE(
                            MAX(
                                CASE
                                    WHEN account_kind = 'buyer_withdraw_pending'
                                    THEN current_balance_usdt
                                END
                            ),
                            0
                        ) AS buyer_withdraw_pending_usdt
                    FROM accounts
                    WHERE owner_user_id = %s
                      AND account_kind IN ('buyer_available', 'buyer_withdraw_pending')
                    """,
                    (buyer_user_id,),
                )
                row = await cur.fetchone()
                return BuyerBalanceSnapshot(
                    buyer_available_usdt=_normalize_amount(row["buyer_available_usdt"]),
                    buyer_withdraw_pending_usdt=_normalize_amount(
                        row["buyer_withdraw_pending_usdt"]
                    ),
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_active_buyer_withdrawal_request(
        self,
        *,
        buyer_user_id: int,
    ) -> ActiveWithdrawalRequestView | None:
        return await self._get_active_withdrawal_request(
            requester_user_id=buyer_user_id,
            requester_role="buyer",
        )

    async def get_active_seller_withdrawal_request(
        self,
        *,
        seller_user_id: int,
    ) -> ActiveWithdrawalRequestView | None:
        return await self._get_active_withdrawal_request(
            requester_user_id=seller_user_id,
            requester_role="seller",
        )

    async def _get_active_withdrawal_request(
        self,
        *,
        requester_user_id: int,
        requester_role: str,
    ) -> ActiveWithdrawalRequestView | None:
        normalized_requester_role = _normalize_requester_role(requester_role)

        async def operation(conn: AsyncConnection) -> ActiveWithdrawalRequestView | None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        wr.id,
                        wr.requester_user_id,
                        wr.requester_role,
                        wr.amount_usdt,
                        wr.status,
                        wr.payout_address,
                        wr.requested_at,
                        wr.processed_at,
                        wr.sent_at,
                        wr.note,
                        p.tx_hash
                    FROM withdrawal_requests wr
                    LEFT JOIN payouts p ON p.withdrawal_request_id = wr.id
                    WHERE wr.requester_user_id = %s
                      AND wr.requester_role = %s
                      AND wr.status = 'withdraw_pending_admin'
                    ORDER BY wr.requested_at DESC, wr.id DESC
                    LIMIT 1
                    """,
                    (requester_user_id, normalized_requester_role),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return ActiveWithdrawalRequestView(
                    withdrawal_request_id=row["id"],
                    requester_user_id=row["requester_user_id"],
                    requester_role=row["requester_role"],
                    amount_usdt=row["amount_usdt"],
                    status=row["status"],
                    payout_address=row["payout_address"],
                    requested_at=row["requested_at"],
                    processed_at=row["processed_at"],
                    sent_at=row["sent_at"],
                    note=row["note"],
                    tx_hash=row["tx_hash"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def count_buyer_withdrawal_history(
        self,
        *,
        buyer_user_id: int,
    ) -> int:
        return await self._count_withdrawal_history(
            requester_user_id=buyer_user_id,
            requester_role="buyer",
        )

    async def count_seller_withdrawal_history(
        self,
        *,
        seller_user_id: int,
    ) -> int:
        return await self._count_withdrawal_history(
            requester_user_id=seller_user_id,
            requester_role="seller",
        )

    async def _count_withdrawal_history(
        self,
        *,
        requester_user_id: int,
        requester_role: str,
    ) -> int:
        normalized_requester_role = _normalize_requester_role(requester_role)

        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS total_count
                    FROM withdrawal_requests
                    WHERE requester_user_id = %s
                      AND requester_role = %s
                    """,
                    (requester_user_id, normalized_requester_role),
                )
                row = await cur.fetchone()
                return int(row["total_count"])

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def list_buyer_withdrawal_history(
        self,
        *,
        buyer_user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WithdrawalHistoryItem]:
        return await self._list_withdrawal_history(
            requester_user_id=buyer_user_id,
            requester_role="buyer",
            limit=limit,
            offset=offset,
        )

    async def list_seller_withdrawal_history(
        self,
        *,
        seller_user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WithdrawalHistoryItem]:
        return await self._list_withdrawal_history(
            requester_user_id=seller_user_id,
            requester_role="seller",
            limit=limit,
            offset=offset,
        )

    async def _list_withdrawal_history(
        self,
        *,
        requester_user_id: int,
        requester_role: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WithdrawalHistoryItem]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        normalized_requester_role = _normalize_requester_role(requester_role)

        async def operation(conn: AsyncConnection) -> list[WithdrawalHistoryItem]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        wr.id,
                        wr.amount_usdt,
                        wr.status,
                        wr.payout_address,
                        wr.requested_at,
                        wr.processed_at,
                        wr.sent_at,
                        wr.note,
                        p.tx_hash
                    FROM withdrawal_requests wr
                    LEFT JOIN payouts p ON p.withdrawal_request_id = wr.id
                    WHERE wr.requester_user_id = %s
                      AND wr.requester_role = %s
                    ORDER BY wr.requested_at DESC, wr.id DESC
                    LIMIT %s
                    OFFSET %s
                    """,
                    (requester_user_id, normalized_requester_role, limit, offset),
                )
                rows = await cur.fetchall()
                return [
                    WithdrawalHistoryItem(
                        withdrawal_request_id=row["id"],
                        amount_usdt=row["amount_usdt"],
                        status=row["status"],
                        payout_address=row["payout_address"],
                        requested_at=row["requested_at"],
                        processed_at=row["processed_at"],
                        sent_at=row["sent_at"],
                        note=row["note"],
                        tx_hash=row["tx_hash"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def list_pending_withdrawals(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PendingWithdrawalView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        async def operation(conn: AsyncConnection) -> list[PendingWithdrawalView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        wr.id,
                        wr.requester_user_id,
                        wr.requester_role,
                        u.telegram_id,
                        u.username,
                        wr.amount_usdt,
                        wr.payout_address,
                        wr.requested_at
                    FROM withdrawal_requests wr
                    JOIN users u ON u.id = wr.requester_user_id
                    WHERE wr.status = 'withdraw_pending_admin'
                    ORDER BY wr.requested_at ASC, wr.id ASC
                    LIMIT %s
                    OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = await cur.fetchall()
                return [
                    PendingWithdrawalView(
                        withdrawal_request_id=row["id"],
                        requester_user_id=row["requester_user_id"],
                        requester_role=row["requester_role"],
                        requester_telegram_id=row["telegram_id"],
                        requester_username=row["username"],
                        amount_usdt=row["amount_usdt"],
                        payout_address=row["payout_address"],
                        requested_at=row["requested_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def count_processed_withdrawals(self) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS total_count
                    FROM withdrawal_requests
                    WHERE status <> 'withdraw_pending_admin'
                    """,
                )
                row = await cur.fetchone()
                return int(row["total_count"])

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def list_processed_withdrawals(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ProcessedWithdrawalView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        async def operation(conn: AsyncConnection) -> list[ProcessedWithdrawalView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        wr.id,
                        wr.requester_user_id,
                        wr.requester_role,
                        u.telegram_id,
                        u.username,
                        wr.amount_usdt,
                        wr.status,
                        wr.payout_address,
                        wr.requested_at,
                        wr.processed_at,
                        wr.sent_at,
                        wr.note,
                        p.tx_hash
                    FROM withdrawal_requests wr
                    JOIN users u ON u.id = wr.requester_user_id
                    LEFT JOIN payouts p ON p.withdrawal_request_id = wr.id
                    WHERE wr.status <> 'withdraw_pending_admin'
                    ORDER BY COALESCE(wr.processed_at, wr.requested_at) DESC, wr.id DESC
                    LIMIT %s
                    OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = await cur.fetchall()
                return [
                    ProcessedWithdrawalView(
                        withdrawal_request_id=row["id"],
                        requester_user_id=row["requester_user_id"],
                        requester_role=row["requester_role"],
                        requester_telegram_id=row["telegram_id"],
                        requester_username=row["username"],
                        amount_usdt=row["amount_usdt"],
                        status=row["status"],
                        payout_address=row["payout_address"],
                        requested_at=row["requested_at"],
                        processed_at=row["processed_at"],
                        sent_at=row["sent_at"],
                        note=row["note"],
                        tx_hash=row["tx_hash"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_withdrawal_request_detail(self, *, request_id: int) -> WithdrawalRequestDetail:
        async def operation(conn: AsyncConnection) -> WithdrawalRequestDetail:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        wr.id,
                        wr.requester_user_id,
                        wr.requester_role,
                        u.telegram_id,
                        u.username,
                        wr.from_account_id,
                        wr.to_account_id,
                        wr.amount_usdt,
                        wr.status,
                        wr.payout_address,
                        wr.requested_at,
                        wr.processed_at,
                        wr.sent_at,
                        wr.note,
                        p.tx_hash
                    FROM withdrawal_requests wr
                    JOIN users u ON u.id = wr.requester_user_id
                    LEFT JOIN payouts p ON p.withdrawal_request_id = wr.id
                    WHERE wr.id = %s
                    """,
                    (request_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"withdrawal request {request_id} not found")
                return WithdrawalRequestDetail(
                    withdrawal_request_id=row["id"],
                    requester_user_id=row["requester_user_id"],
                    requester_role=row["requester_role"],
                    requester_telegram_id=row["telegram_id"],
                    requester_username=row["username"],
                    from_account_id=row["from_account_id"],
                    to_account_id=row["to_account_id"],
                    amount_usdt=row["amount_usdt"],
                    status=row["status"],
                    payout_address=row["payout_address"],
                    requested_at=row["requested_at"],
                    processed_at=row["processed_at"],
                    sent_at=row["sent_at"],
                    note=row["note"],
                    tx_hash=row["tx_hash"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

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
    ) -> TransferResult:
        if from_account_id == to_account_id:
            raise InvalidStateError("transfer source and destination must differ")

        await cur.execute(
            """
            SELECT id
            FROM ledger_entries
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            return TransferResult(entry_id=existing["id"], created=False)

        locked_ids = sorted([from_account_id, to_account_id])
        await cur.execute(
            """
            SELECT id
            FROM accounts
            WHERE id = ANY(%s)
            ORDER BY id
            FOR UPDATE
            """,
            (locked_ids,),
        )
        accounts = await cur.fetchall()
        if len(accounts) != 2:
            raise NotFoundError("one or more accounts not found")

        await cur.execute(
            """
            UPDATE accounts
            SET current_balance_usdt = current_balance_usdt - %s,
                updated_at = timezone('utc', now())
            WHERE id = %s
                AND current_balance_usdt >= %s
            RETURNING id
            """,
            (amount_usdt, from_account_id, amount_usdt),
        )
        debited = await cur.fetchone()
        if debited is None:
            raise InsufficientFundsError("source account has insufficient funds")

        await cur.execute(
            """
            UPDATE accounts
            SET current_balance_usdt = current_balance_usdt + %s,
                updated_at = timezone('utc', now())
            WHERE id = %s
            RETURNING id
            """,
            (amount_usdt, to_account_id),
        )
        credited = await cur.fetchone()
        if credited is None:
            raise NotFoundError("destination account not found")

        await cur.execute(
            """
            INSERT INTO ledger_entries (
                event_type,
                idempotency_key,
                entity_type,
                entity_id,
                metadata_json
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (event_type, idempotency_key, entity_type, entity_id, Json(metadata)),
        )
        entry = await cur.fetchone()

        await cur.execute(
            """
            INSERT INTO ledger_postings (
                entry_id,
                account_id,
                direction,
                amount_usdt
            )
            VALUES
                (%s, %s, 'debit', %s),
                (%s, %s, 'credit', %s)
            """,
            (
                entry["id"],
                from_account_id,
                amount_usdt,
                entry["id"],
                to_account_id,
                amount_usdt,
            ),
        )

        return TransferResult(entry_id=entry["id"], created=True)

    async def _ensure_system_or_owner_account_locked(
        self,
        cur,
        *,
        owner_user_id: int | None,
        account_kind: str,
    ) -> int:
        account_code = (
            f"user:{owner_user_id}:{account_kind}"
            if owner_user_id is not None
            else f"system:{account_kind}"
        )
        await cur.execute(
            """
            INSERT INTO accounts (
                owner_user_id,
                account_code,
                account_kind
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (account_code)
            DO UPDATE SET
                owner_user_id = EXCLUDED.owner_user_id,
                updated_at = timezone('utc', now())
            RETURNING id
            """,
            (owner_user_id, account_code, account_kind),
        )
        row = await cur.fetchone()
        return row["id"]

    async def _ensure_system_account(self, cur, *, account_kind: str) -> int:
        return await self.ensure_system_account_locked(cur, account_kind=account_kind)

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
        await cur.execute(
            """
            SELECT id
            FROM balance_holds
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            return existing["id"]

        await cur.execute(
            """
            INSERT INTO balance_holds (
                account_id,
                hold_type,
                status,
                amount_usdt,
                listing_id,
                assignment_id,
                withdrawal_request_id,
                idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                account_id,
                hold_type,
                status,
                amount_usdt,
                listing_id,
                assignment_id,
                withdrawal_request_id,
                idempotency_key,
            ),
        )
        hold = await cur.fetchone()
        return hold["id"]

    async def _insert_admin_audit(
        self,
        cur,
        *,
        admin_user_id: int,
        action: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        await cur.execute(
            """
            INSERT INTO admin_audit_actions (
                admin_user_id,
                action,
                target_type,
                target_id,
                payload_json,
                idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key)
            DO NOTHING
            """,
            (admin_user_id, action, target_type, target_id, Json(payload), idempotency_key),
        )


def _ledger_key(idempotency_key: str) -> str:
    return f"ledger:{idempotency_key}"


def _hold_key(idempotency_key: str) -> str:
    return f"hold:{idempotency_key}"


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _normalize_requester_role(requester_role: str) -> str:
    normalized = requester_role.strip().lower()
    if normalized not in _WITHDRAWAL_REQUESTER_ROLES:
        raise ValueError("requester_role must be buyer|seller")
    return normalized
