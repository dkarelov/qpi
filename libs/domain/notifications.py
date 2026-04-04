from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.models import NotificationOutboxItem

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_SENDING = "sending"
OUTBOX_STATUS_SENT = "sent"
OUTBOX_STATUS_FAILED_PERMANENT = "failed_permanent"

EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER = "assignment_reservation_expired_buyer"
EVENT_ASSIGNMENT_ORDER_VERIFIED_SELLER = "assignment_order_verified_seller"
EVENT_ASSIGNMENT_PICKED_UP_BUYER = "assignment_picked_up_buyer"
EVENT_ASSIGNMENT_PICKED_UP_SELLER = "assignment_picked_up_seller"
EVENT_ASSIGNMENT_RETURNED_BUYER = "assignment_returned_buyer"
EVENT_ASSIGNMENT_RETURNED_SELLER = "assignment_returned_seller"
EVENT_ASSIGNMENT_DELIVERY_EXPIRED_BUYER = "assignment_delivery_expired_buyer"
EVENT_ASSIGNMENT_DELIVERY_EXPIRED_SELLER = "assignment_delivery_expired_seller"
EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER = "assignment_review_confirmed_seller"
EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER = "assignment_reward_unlocked_buyer"
EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER = "assignment_reward_unlocked_seller"
EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER = "assignment_early_payout_listing_delete_buyer"
EVENT_ASSIGNMENT_EARLY_PAYOUT_SHOP_DELETE_BUYER = "assignment_early_payout_shop_delete_buyer"
EVENT_SELLER_TOKEN_INVALIDATED = "seller_token_invalidated"
EVENT_DEPOSIT_CREDITED_SELLER = "deposit_credited_seller"
EVENT_DEPOSIT_MANUAL_REVIEW_SELLER = "deposit_manual_review_seller"
EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN = "deposit_manual_review_admin"
EVENT_DEPOSIT_EXPIRED_SELLER = "deposit_expired_seller"
EVENT_DEPOSIT_CANCELLED_SELLER = "deposit_cancelled_seller"
EVENT_WITHDRAW_CREATED_ADMIN = "withdraw_created_admin"
EVENT_WITHDRAW_CANCELLED_ADMIN = "withdraw_cancelled_admin"
EVENT_WITHDRAW_REJECTED_REQUESTER = "withdraw_rejected_requester"
EVENT_WITHDRAW_SENT_REQUESTER = "withdraw_sent_requester"
EVENT_MANUAL_BALANCE_CREDIT_TARGET = "manual_balance_credit_target"


class NotificationService:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def sync_admin_users(self, *, telegram_ids: Sequence[int]) -> None:
        normalized_ids = sorted({int(item) for item in telegram_ids if int(item) > 0})
        if not normalized_ids:
            return

        async def operation(conn) -> None:
            async with conn.cursor(row_factory=dict_row) as cur:
                for telegram_id in normalized_ids:
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
                            VALUES (%s, NULL, 'admin', false, false, true)
                            """,
                            (telegram_id,),
                        )
                        continue
                    await cur.execute(
                        """
                        UPDATE users
                        SET is_admin = true,
                            updated_at = timezone('utc', now())
                        WHERE id = %s
                        """,
                        (existing["id"],),
                    )

        await run_in_transaction(self._pool, operation)

    async def enqueue_locked(
        self,
        cur,
        *,
        recipient_telegram_id: int,
        recipient_scope: str,
        event_type: str,
        dedupe_key: str,
        payload_json: dict[str, Any],
    ) -> None:
        await cur.execute(
            """
            INSERT INTO notification_outbox (
                recipient_telegram_id,
                recipient_scope,
                event_type,
                dedupe_key,
                payload_json,
                status,
                next_attempt_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, timezone('utc', now()))
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            (
                recipient_telegram_id,
                recipient_scope,
                event_type,
                dedupe_key,
                Json(_json_compatible(payload_json)),
                OUTBOX_STATUS_PENDING,
            ),
        )

    async def enqueue_admins_locked(
        self,
        cur,
        *,
        event_type: str,
        dedupe_key_prefix: str,
        payload_json: dict[str, Any],
    ) -> None:
        admin_ids = await self._list_admin_telegram_ids_locked(cur)
        for telegram_id in admin_ids:
            await self.enqueue_locked(
                cur,
                recipient_telegram_id=telegram_id,
                recipient_scope="admin",
                event_type=event_type,
                dedupe_key=f"{dedupe_key_prefix}:admin:{telegram_id}",
                payload_json=payload_json,
            )

    async def enqueue_assignment_reservation_expired_for_buyer_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["buyer_telegram_id"],
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_RESERVATION_EXPIRED_BUYER,
            dedupe_key=f"assignment:{assignment_id}:expired_2h:buyer",
            payload_json=self._assignment_payload(ctx),
        )

    async def enqueue_assignment_order_verified_for_seller_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        payload["order_id"] = ctx["order_id"]
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_ORDER_VERIFIED_SELLER,
            dedupe_key=f"assignment:{assignment_id}:order_verified:seller",
            payload_json=payload,
        )

    async def enqueue_assignment_picked_up_locked(self, cur, *, assignment_id: int) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        payload["unlock_at"] = _iso_or_none(ctx["unlock_at"])
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["buyer_telegram_id"],
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_PICKED_UP_BUYER,
            dedupe_key=f"assignment:{assignment_id}:picked_up_wait_unlock:buyer",
            payload_json=payload,
        )
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_PICKED_UP_SELLER,
            dedupe_key=f"assignment:{assignment_id}:picked_up_wait_unlock:seller",
            payload_json=payload,
        )

    async def enqueue_assignment_returned_locked(self, cur, *, assignment_id: int) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["buyer_telegram_id"],
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_RETURNED_BUYER,
            dedupe_key=f"assignment:{assignment_id}:returned_within_14d:buyer",
            payload_json=payload,
        )
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_RETURNED_SELLER,
            dedupe_key=f"assignment:{assignment_id}:returned_within_14d:seller",
            payload_json=payload,
        )

    async def enqueue_assignment_delivery_expired_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["buyer_telegram_id"],
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_DELIVERY_EXPIRED_BUYER,
            dedupe_key=f"assignment:{assignment_id}:delivery_expired:buyer",
            payload_json=payload,
        )
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_DELIVERY_EXPIRED_SELLER,
            dedupe_key=f"assignment:{assignment_id}:delivery_expired:seller",
            payload_json=payload,
        )

    async def enqueue_assignment_review_confirmed_for_seller_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> None:
        ctx = await self._load_assignment_review_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        payload["reviewed_at"] = _iso_or_none(ctx["reviewed_at"])
        payload["rating"] = int(ctx["rating"])
        payload["review_text"] = ctx["review_text"] or ""
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REVIEW_CONFIRMED_SELLER,
            dedupe_key=f"assignment:{assignment_id}:review_confirmed:seller",
            payload_json=payload,
        )

    async def enqueue_assignment_reward_unlocked_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> None:
        ctx = await self._load_assignment_context_locked(cur, assignment_id=assignment_id)
        payload = self._assignment_payload(ctx)
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["buyer_telegram_id"],
            recipient_scope="buyer",
            event_type=EVENT_ASSIGNMENT_REWARD_UNLOCKED_BUYER,
            dedupe_key=f"assignment:{assignment_id}:reward_unlocked:buyer",
            payload_json=payload,
        )
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_ASSIGNMENT_REWARD_UNLOCKED_SELLER,
            dedupe_key=f"assignment:{assignment_id}:reward_unlocked:seller",
            payload_json=payload,
        )

    async def enqueue_buyer_early_payout_locked(
        self,
        cur,
        *,
        buyer_telegram_id: int,
        scope: str,
        scope_id: int,
        shop_title: str,
        item_count: int,
        total_reward_usdt: Decimal,
    ) -> None:
        event_type = (
            EVENT_ASSIGNMENT_EARLY_PAYOUT_SHOP_DELETE_BUYER
            if scope == "shop"
            else EVENT_ASSIGNMENT_EARLY_PAYOUT_LISTING_DELETE_BUYER
        )
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=buyer_telegram_id,
            recipient_scope="buyer",
            event_type=event_type,
            dedupe_key=f"{scope}:{scope_id}:early_payout:buyer:{buyer_telegram_id}",
            payload_json={
                "shop_title": shop_title,
                "item_count": item_count,
                "total_reward_usdt": str(_normalize_amount(total_reward_usdt)),
            },
        )

    async def enqueue_seller_token_invalidated_locked(
        self,
        cur,
        *,
        shop_id: int,
        paused_listings_count: int,
        source: str,
    ) -> None:
        await cur.execute(
            """
            SELECT s.title, u.telegram_id AS seller_telegram_id
            FROM shops s
            JOIN users u ON u.id = s.seller_user_id
            WHERE s.id = %s
            """,
            (shop_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=row["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_SELLER_TOKEN_INVALIDATED,
            dedupe_key=f"shop:{shop_id}:token_invalidated:{source}:{paused_listings_count}",
            payload_json={
                "shop_id": shop_id,
                "shop_title": row["title"],
                "paused_listings_count": paused_listings_count,
                "source": source,
            },
        )

    async def enqueue_deposit_credited_locked(
        self,
        cur,
        *,
        deposit_intent_id: int,
    ) -> None:
        ctx = await self._load_deposit_intent_context_locked(
            cur, deposit_intent_id=deposit_intent_id
        )
        if ctx is None:
            return
        amount = ctx["credited_amount_usdt"] or ctx["tx_amount_usdt"] or Decimal("0")
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_DEPOSIT_CREDITED_SELLER,
            dedupe_key=f"deposit_intent:{deposit_intent_id}:credited:seller",
            payload_json={
                "deposit_intent_id": deposit_intent_id,
                "amount_usdt": str(_normalize_amount(amount)),
                "tx_hash": ctx["tx_hash"],
            },
        )

    async def enqueue_deposit_manual_review_locked(
        self,
        cur,
        *,
        chain_tx_id: int,
        matched_intent_id: int | None,
        reason: str,
    ) -> None:
        await cur.execute(
            """
            SELECT
                tx.tx_hash,
                tx.amount_usdt,
                di.id AS deposit_intent_id,
                u.telegram_id AS seller_telegram_id
            FROM chain_incoming_txs tx
            LEFT JOIN deposit_intents di ON di.id = tx.matched_intent_id
            LEFT JOIN users u ON u.id = di.seller_user_id
            WHERE tx.id = %s
            """,
            (chain_tx_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return
        payload = {
            "chain_tx_id": chain_tx_id,
            "deposit_intent_id": matched_intent_id,
            "tx_hash": row["tx_hash"],
            "amount_usdt": str(_normalize_amount(row["amount_usdt"])),
            "reason": reason,
        }
        if row["seller_telegram_id"] is not None and matched_intent_id is not None:
            await self.enqueue_locked(
                cur,
                recipient_telegram_id=row["seller_telegram_id"],
                recipient_scope="seller",
                event_type=EVENT_DEPOSIT_MANUAL_REVIEW_SELLER,
                dedupe_key=f"chain_tx:{chain_tx_id}:manual_review:seller",
                payload_json=payload,
            )
        await self.enqueue_admins_locked(
            cur,
            event_type=EVENT_DEPOSIT_MANUAL_REVIEW_ADMIN,
            dedupe_key_prefix=f"chain_tx:{chain_tx_id}:manual_review",
            payload_json=payload,
        )

    async def enqueue_deposit_expired_locked(
        self,
        cur,
        *,
        deposit_intent_id: int,
    ) -> None:
        ctx = await self._load_deposit_intent_context_locked(
            cur, deposit_intent_id=deposit_intent_id
        )
        if ctx is None:
            return
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_DEPOSIT_EXPIRED_SELLER,
            dedupe_key=f"deposit_intent:{deposit_intent_id}:expired:seller",
            payload_json={
                "deposit_intent_id": deposit_intent_id,
                "expected_amount_usdt": str(_normalize_amount(ctx["expected_amount_usdt"])),
            },
        )

    async def enqueue_deposit_cancelled_locked(
        self,
        cur,
        *,
        deposit_intent_id: int,
    ) -> None:
        ctx = await self._load_deposit_intent_context_locked(
            cur, deposit_intent_id=deposit_intent_id
        )
        if ctx is None:
            return
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=ctx["seller_telegram_id"],
            recipient_scope="seller",
            event_type=EVENT_DEPOSIT_CANCELLED_SELLER,
            dedupe_key=f"deposit_intent:{deposit_intent_id}:cancelled:seller",
            payload_json={
                "deposit_intent_id": deposit_intent_id,
                "reason": ctx["review_reason"],
            },
        )

    async def enqueue_withdraw_created_for_admins_locked(self, cur, *, request_id: int) -> None:
        payload = await self._load_withdraw_request_context_locked(cur, request_id=request_id)
        if payload is None:
            return
        await self.enqueue_admins_locked(
            cur,
            event_type=EVENT_WITHDRAW_CREATED_ADMIN,
            dedupe_key_prefix=f"withdrawal_request:{request_id}:created",
            payload_json=payload,
        )

    async def enqueue_withdraw_cancelled_for_admins_locked(self, cur, *, request_id: int) -> None:
        payload = await self._load_withdraw_request_context_locked(cur, request_id=request_id)
        if payload is None:
            return
        await self.enqueue_admins_locked(
            cur,
            event_type=EVENT_WITHDRAW_CANCELLED_ADMIN,
            dedupe_key_prefix=f"withdrawal_request:{request_id}:cancelled",
            payload_json=payload,
        )

    async def enqueue_withdraw_status_for_requester_locked(
        self,
        cur,
        *,
        request_id: int,
        event_type: str,
    ) -> None:
        payload = await self._load_withdraw_request_context_locked(cur, request_id=request_id)
        if payload is None:
            return
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=payload["requester_telegram_id"],
            recipient_scope=payload["requester_role"],
            event_type=event_type,
            dedupe_key=f"withdrawal_request:{request_id}:{event_type}:{payload['requester_role']}",
            payload_json=payload,
        )

    async def enqueue_manual_balance_credit_locked(
        self,
        cur,
        *,
        target_user_id: int,
        amount_usdt: Decimal,
        recipient_role: str,
        dedupe_key: str,
    ) -> None:
        await cur.execute(
            """
            SELECT telegram_id
            FROM users
            WHERE id = %s
            """,
            (target_user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return
        await self.enqueue_locked(
            cur,
            recipient_telegram_id=row["telegram_id"],
            recipient_scope=recipient_role,
            event_type=EVENT_MANUAL_BALANCE_CREDIT_TARGET,
            dedupe_key=dedupe_key,
            payload_json={
                "amount_usdt": str(_normalize_amount(amount_usdt)),
                "recipient_role": recipient_role,
            },
        )

    async def claim_pending(self, *, limit: int) -> list[NotificationOutboxItem]:
        if limit < 1:
            return []

        async def operation(conn) -> list[NotificationOutboxItem]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM notification_outbox
                        WHERE (
                                status = %s
                                AND next_attempt_at <= timezone('utc', now())
                              )
                           OR (
                                status = %s
                                AND updated_at <= timezone('utc', now()) - interval '5 minutes'
                              )
                        ORDER BY created_at ASC, id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE notification_outbox n
                    SET status = %s,
                        updated_at = timezone('utc', now())
                    FROM candidate
                    WHERE n.id = candidate.id
                    RETURNING
                        n.id,
                        n.recipient_telegram_id,
                        n.recipient_scope,
                        n.event_type,
                        n.dedupe_key,
                        n.payload_json,
                        n.status,
                        n.attempt_count,
                        n.next_attempt_at,
                        n.last_error,
                        n.sent_at,
                        n.created_at,
                        n.updated_at
                    """,
                    (
                        OUTBOX_STATUS_PENDING,
                        OUTBOX_STATUS_SENDING,
                        limit,
                        OUTBOX_STATUS_SENDING,
                    ),
                )
                rows = await cur.fetchall()
                return [self._row_to_item(row) for row in rows]

        return await run_in_transaction(self._pool, operation)

    async def mark_sent(self, *, notification_id: int) -> None:
        async def operation(conn) -> None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE notification_outbox
                    SET status = %s,
                        sent_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (OUTBOX_STATUS_SENT, notification_id),
                )

        await run_in_transaction(self._pool, operation)

    async def mark_retry(self, *, notification_id: int, error: str, delay_seconds: int) -> None:
        normalized_error = (error or "")[:500]
        delay = max(1, int(delay_seconds))

        async def operation(conn) -> None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE notification_outbox
                    SET status = %s,
                        attempt_count = attempt_count + 1,
                        last_error = %s,
                        next_attempt_at = timezone('utc', now()) + (%s * interval '1 second'),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (OUTBOX_STATUS_PENDING, normalized_error, delay, notification_id),
                )

        await run_in_transaction(self._pool, operation)

    async def mark_failed_permanent(self, *, notification_id: int, error: str) -> None:
        normalized_error = (error or "")[:500]

        async def operation(conn) -> None:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE notification_outbox
                    SET status = %s,
                        attempt_count = attempt_count + 1,
                        last_error = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (OUTBOX_STATUS_FAILED_PERMANENT, normalized_error, notification_id),
                )

        await run_in_transaction(self._pool, operation)

    async def _list_admin_telegram_ids_locked(self, cur) -> list[int]:
        await cur.execute(
            """
            SELECT telegram_id
            FROM users
            WHERE is_admin = true
            ORDER BY telegram_id ASC
            """
        )
        return [int(row["telegram_id"]) for row in await cur.fetchall()]

    async def _load_assignment_context_locked(self, cur, *, assignment_id: int) -> dict[str, Any]:
        await cur.execute(
            """
            SELECT
                a.id AS assignment_id,
                a.reward_usdt,
                a.unlock_at,
                a.order_id,
                a.review_required,
                bu.telegram_id AS buyer_telegram_id,
                l.id AS listing_id,
                l.display_title,
                su.telegram_id AS seller_telegram_id,
                s.id AS shop_id,
                s.title AS shop_title
            FROM assignments a
            JOIN users bu ON bu.id = a.buyer_user_id
            JOIN listings l ON l.id = a.listing_id
            JOIN users su ON su.id = l.seller_user_id
            JOIN shops s ON s.id = l.shop_id
            WHERE a.id = %s
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"assignment {assignment_id} not found for notification")
        return dict(row)

    async def _load_assignment_review_context_locked(
        self,
        cur,
        *,
        assignment_id: int,
    ) -> dict[str, Any]:
        await cur.execute(
            """
            SELECT
                a.id AS assignment_id,
                a.reward_usdt,
                a.unlock_at,
                a.review_required,
                bu.telegram_id AS buyer_telegram_id,
                l.id AS listing_id,
                l.display_title,
                su.telegram_id AS seller_telegram_id,
                s.id AS shop_id,
                s.title AS shop_title,
                br.reviewed_at,
                br.rating,
                br.review_text
            FROM assignments a
            JOIN buyer_reviews br ON br.assignment_id = a.id
            JOIN users bu ON bu.id = a.buyer_user_id
            JOIN listings l ON l.id = a.listing_id
            JOIN users su ON su.id = l.seller_user_id
            JOIN shops s ON s.id = l.shop_id
            WHERE a.id = %s
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"assignment {assignment_id} not found for review notification")
        return dict(row)

    async def _load_deposit_intent_context_locked(
        self,
        cur,
        *,
        deposit_intent_id: int,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                di.id,
                di.expected_amount_usdt,
                di.credited_amount_usdt,
                di.review_reason,
                u.telegram_id AS seller_telegram_id,
                tx.tx_hash,
                tx.amount_usdt AS tx_amount_usdt
            FROM deposit_intents di
            JOIN users u ON u.id = di.seller_user_id
            LEFT JOIN chain_incoming_txs tx ON tx.id = di.matched_chain_tx_id
            WHERE di.id = %s
            """,
            (deposit_intent_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _load_withdraw_request_context_locked(
        self,
        cur,
        *,
        request_id: int,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                wr.id AS withdrawal_request_id,
                wr.requester_role,
                wr.amount_usdt,
                wr.status,
                wr.payout_address,
                wr.requested_at,
                wr.processed_at,
                wr.sent_at,
                wr.note,
                u.telegram_id AS requester_telegram_id,
                u.username AS requester_username,
                p.tx_hash
            FROM withdrawal_requests wr
            JOIN users u ON u.id = wr.requester_user_id
            LEFT JOIN payouts p ON p.withdrawal_request_id = wr.id
            WHERE wr.id = %s
            """,
            (request_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    def _assignment_payload(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "assignment_id": ctx["assignment_id"],
            "listing_id": ctx["listing_id"],
            "shop_id": ctx["shop_id"],
            "display_title": ctx["display_title"] or "Без названия",
            "shop_title": ctx["shop_title"] or "Магазин",
            "reward_usdt": str(_normalize_amount(ctx["reward_usdt"])),
            "review_required": bool(ctx.get("review_required", False)),
        }

    def _row_to_item(self, row: dict[str, Any]) -> NotificationOutboxItem:
        return NotificationOutboxItem(
            notification_id=row["id"],
            recipient_telegram_id=row["recipient_telegram_id"],
            recipient_scope=row["recipient_scope"],
            event_type=row["event_type"],
            dedupe_key=row["dedupe_key"],
            payload_json=dict(row["payload_json"] or {}),
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            sent_at=row["sent_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(_normalize_amount(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    return value


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
