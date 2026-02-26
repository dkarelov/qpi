from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import AsyncConnection
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
    AssignmentReservationResult,
    StatusChangeResult,
    TransferResult,
    WithdrawalRequestResult,
)

_CANCELLATION_STATES = {"expired_2h", "wb_invalid", "returned_within_14d", "delivery_expired"}


class FinanceService:
    """Transactional money and assignment primitives using plain SQL."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

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
        reservation_timeout_hours: int = 2,
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
                    SELECT id, status, reward_usdt, available_slots, deleted_at
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

                await cur.execute(
                    """
                    INSERT INTO assignments (
                        listing_id,
                        buyer_user_id,
                        status,
                        reward_usdt,
                        reservation_expires_at,
                        idempotency_key
                    )
                    VALUES (
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
                        reward_usdt,
                        reservation_timeout_hours,
                        idempotency_key,
                    ),
                )
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
                    "order_submitted",
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

                if assignment["status"] == "eligible_for_withdrawal":
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
                    SET status = 'eligible_for_withdrawal',
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

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def create_withdrawal_request(
        self,
        *,
        buyer_user_id: int,
        from_account_id: int,
        pending_account_id: int,
        amount_usdt: Decimal,
        payout_address: str,
        idempotency_key: str,
    ) -> WithdrawalRequestResult:
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

                await self._transfer_locked(
                    cur,
                    from_account_id=from_account_id,
                    to_account_id=pending_account_id,
                    amount_usdt=amount,
                    event_type="withdraw_request",
                    idempotency_key=_ledger_key(idempotency_key),
                    entity_type="withdrawal_request",
                    entity_id=None,
                    metadata={"buyer_user_id": buyer_user_id},
                )

                await cur.execute(
                    """
                    INSERT INTO withdrawal_requests (
                        buyer_user_id,
                        from_account_id,
                        to_account_id,
                        amount_usdt,
                        status,
                        payout_address,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, 'withdraw_pending_admin', %s, %s)
                    RETURNING id
                    """,
                    (
                        buyer_user_id,
                        from_account_id,
                        pending_account_id,
                        amount,
                        payout_address,
                        idempotency_key,
                    ),
                )
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

                return WithdrawalRequestResult(
                    withdrawal_request_id=withdrawal_request["id"],
                    created=True,
                )

        return await run_in_transaction(self._pool, operation)

    async def approve_withdrawal_request(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status
                    FROM withdrawal_requests
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (request_id,),
                )
                request = await cur.fetchone()
                if request is None:
                    raise NotFoundError(f"withdrawal request {request_id} not found")

                if request["status"] == "approved":
                    return StatusChangeResult(changed=False)

                if request["status"] != "withdraw_pending_admin":
                    raise InvalidStateError("withdrawal request must be pending admin")

                await cur.execute(
                    """
                    UPDATE withdrawal_requests
                    SET status = 'approved',
                        admin_user_id = %s,
                        processed_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (admin_user_id, request_id),
                )

                await self._insert_admin_audit(
                    cur,
                    admin_user_id=admin_user_id,
                    action="withdraw_approved",
                    target_type="withdrawal_request",
                    target_id=str(request_id),
                    payload={"request_id": request_id},
                    idempotency_key=idempotency_key,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def reject_withdrawal_request(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        pending_account_id: int,
        buyer_available_account_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status, amount_usdt
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

                if request["status"] not in {"withdraw_pending_admin", "approved"}:
                    raise InvalidStateError(
                        "withdrawal request cannot be rejected from current state"
                    )

                await cur.execute(
                    """
                    UPDATE withdrawal_requests
                    SET status = 'rejected',
                        admin_user_id = %s,
                        processed_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (admin_user_id, request_id),
                )

                await self._transfer_locked(
                    cur,
                    from_account_id=pending_account_id,
                    to_account_id=buyer_available_account_id,
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
                    payload={"request_id": request_id},
                    idempotency_key=idempotency_key,
                )

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def mark_withdrawal_sent(
        self,
        *,
        request_id: int,
        admin_user_id: int,
        pending_account_id: int,
        system_payout_account_id: int,
        tx_hash: str,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status, amount_usdt
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

                if request["status"] != "approved":
                    raise InvalidStateError("withdrawal request must be approved before sending")

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
                    from_account_id=pending_account_id,
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

                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

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
