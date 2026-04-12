from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.errors import (
    DuplicateOrderError,
    InvalidStateError,
    NotFoundError,
    PayloadValidationError,
)
from libs.domain.ledger import FinanceService
from libs.domain.models import (
    AdminPendingReviewConfirmationView,
    AdminReviewVerificationResult,
    AssignmentReservationResult,
    BuyerAssignmentView,
    BuyerBootstrapResult,
    BuyerListingResult,
    BuyerOrderSubmitResult,
    BuyerReviewSubmitResult,
    BuyerSavedShopResult,
    BuyerShopResult,
    ReservationExpiryResult,
    StatusChangeResult,
)
from libs.domain.notifications import NotificationService

_ACTIVE_ASSIGNMENT_STATES = (
    "reserved",
    "order_verified",
    "picked_up_wait_review",
    "picked_up_wait_unlock",
    "withdraw_sent",
)
_ASSIGNMENT_PAYLOAD_ALLOWED_STATES = {"reserved", "order_verified"}
_RESERVATION_EXPIRED_STATUS = "expired_2h"
_BUYER_CANCELLED_STATUS = "buyer_cancelled"
_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX = "reservation-expire"
_PURCHASE_PAYLOAD_VERSION = 3
_REVIEW_PAYLOAD_VERSION = 2
_PURCHASE_TOKEN_TYPE = 1
_REVIEW_TOKEN_TYPE = 2
_REVIEW_STATUS_PENDING_MANUAL = "pending_manual"
_REVIEW_STATUS_VERIFIED_AUTO = "verified_auto"
_REVIEW_STATUS_VERIFIED_ADMIN = "verified_admin"
_REVIEW_NORMALIZE_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DecodedPurchasePayload:
    payload_version: int
    task_uuid: UUID
    order_id: str
    ordered_at: datetime
    wb_product_id: int
    raw_payload_json: list[Any]


@dataclass(frozen=True)
class DecodedReviewPayload:
    payload_version: int
    task_uuid: UUID
    wb_product_id: int
    reviewed_at: datetime
    rating: int
    review_text: str
    raw_payload_json: list[Any]


class BuyerService:
    """Buyer lifecycle operations implemented with plain SQL transactions."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        finance_service: FinanceService | None = None,
    ) -> None:
        self._pool = pool
        self._finance_service = finance_service or FinanceService(pool)
        self._notifications = NotificationService(pool)

    async def bootstrap_buyer(
        self,
        *,
        telegram_id: int,
        username: str | None,
    ) -> BuyerBootstrapResult:
        async def operation(conn: AsyncConnection) -> BuyerBootstrapResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, role, is_buyer, is_admin
                    FROM users
                    WHERE telegram_id = %s
                    FOR UPDATE
                    """,
                    (telegram_id,),
                )
                existing = await cur.fetchone()
                created_user = False
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
                        VALUES (%s, %s, 'buyer', false, true, false)
                        RETURNING id
                        """,
                        (telegram_id, username),
                    )
                    created = await cur.fetchone()
                    user_id = created["id"]
                    created_user = True
                else:
                    user_id = existing["id"]
                    await cur.execute(
                        """
                        UPDATE users
                        SET username = COALESCE(%s, username),
                            is_buyer = true,
                            is_admin = is_admin OR role = 'admin',
                            updated_at = timezone('utc', now())
                        WHERE id = %s
                        """,
                        (username, user_id),
                    )

                buyer_available_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=user_id,
                    account_kind="buyer_available",
                )
                buyer_withdraw_pending_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=user_id,
                    account_kind="buyer_withdraw_pending",
                )

                return BuyerBootstrapResult(
                    user_id=user_id,
                    created_user=created_user,
                    buyer_available_account_id=buyer_available_account_id,
                    buyer_withdraw_pending_account_id=buyer_withdraw_pending_account_id,
                )

        return await run_in_transaction(self._pool, operation)

    async def resolve_shop_by_slug(self, *, slug: str) -> BuyerShopResult:
        normalized_slug = slug.strip()
        if not normalized_slug:
            raise ValueError("slug must not be empty")

        async def operation(conn: AsyncConnection) -> BuyerShopResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, slug, title
                    FROM shops
                    WHERE slug = %s
                      AND deleted_at IS NULL
                    """,
                    (normalized_slug,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"shop slug '{normalized_slug}' not found")
                return BuyerShopResult(
                    shop_id=row["id"],
                    slug=row["slug"],
                    title=row["title"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def list_active_listings_by_shop_slug(
        self,
        *,
        slug: str,
        buyer_user_id: int | None = None,
    ) -> list[BuyerListingResult]:
        shop = await self.resolve_shop_by_slug(slug=slug)

        async def operation(conn: AsyncConnection) -> list[BuyerListingResult]:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: list[Any] = [shop.shop_id]
                buyer_filters = ""
                if buyer_user_id is not None:
                    buyer_filters = """
                      AND NOT EXISTS (
                            SELECT 1
                            FROM assignments ax
                            JOIN listings lx ON lx.id = ax.listing_id
                            WHERE ax.buyer_user_id = %s
                              AND lx.wb_product_id = l.wb_product_id
                              AND ax.status = ANY (
                                    ARRAY[
                                        'reserved'::text,
                                        'order_verified'::text,
                                        'picked_up_wait_review'::text,
                                        'picked_up_wait_unlock'::text,
                                        'withdraw_sent'::text
                                    ]
                              )
                      )
                      AND NOT EXISTS (
                            SELECT 1
                            FROM buyer_orders bo
                            WHERE bo.buyer_user_id = %s
                              AND bo.wb_product_id = l.wb_product_id
                      )
                    """
                    params.extend([buyer_user_id, buyer_user_id])

                await cur.execute(
                    f"""
                    SELECT
                        l.id,
                        l.shop_id,
                        l.wb_product_id,
                        l.display_title,
                        l.wb_source_title,
                        l.reference_price_rub,
                        l.wb_subject_name,
                        l.wb_brand_name,
                        l.wb_description,
                        l.wb_photo_url,
                        l.wb_tech_sizes_json,
                        l.wb_characteristics_json,
                        l.search_phrase,
                        l.reward_usdt,
                        l.slot_count,
                        l.available_slots
                    FROM listings l
                    WHERE l.shop_id = %s
                      AND l.deleted_at IS NULL
                      AND l.status = 'active'
                      AND l.available_slots > 0
                      {buyer_filters}
                    ORDER BY l.created_at ASC
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
                return [
                    BuyerListingResult(
                        listing_id=row["id"],
                        shop_id=row["shop_id"],
                        wb_product_id=row["wb_product_id"],
                        display_title=row["display_title"],
                        wb_source_title=row["wb_source_title"],
                        reference_price_rub=row["reference_price_rub"],
                        wb_subject_name=row["wb_subject_name"],
                        wb_brand_name=row["wb_brand_name"],
                        wb_description=row["wb_description"],
                        wb_photo_url=row["wb_photo_url"],
                        wb_tech_sizes=list(row["wb_tech_sizes_json"] or []),
                        wb_characteristics=list(row["wb_characteristics_json"] or []),
                        search_phrase=row["search_phrase"],
                        reward_usdt=row["reward_usdt"],
                        slot_count=row["slot_count"],
                        available_slots=row["available_slots"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def touch_saved_shop(
        self,
        *,
        buyer_user_id: int,
        shop_id: int,
    ) -> None:
        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM shops
                    WHERE id = %s
                      AND deleted_at IS NULL
                    """,
                    (shop_id,),
                )
                if await cur.fetchone() is None:
                    raise NotFoundError(f"shop {shop_id} not found")

                await cur.execute(
                    """
                    INSERT INTO buyer_saved_shops (
                        buyer_user_id,
                        shop_id
                    )
                    VALUES (%s, %s)
                    ON CONFLICT (buyer_user_id, shop_id)
                    DO UPDATE SET
                        last_opened_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    """,
                    (buyer_user_id, shop_id),
                )

        await run_in_transaction(self._pool, operation)

    async def list_saved_shops(
        self,
        *,
        buyer_user_id: int,
        limit: int = 20,
    ) -> list[BuyerSavedShopResult]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[BuyerSavedShopResult]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        s.id,
                        s.slug,
                        s.title,
                        bss.last_opened_at,
                        (
                            SELECT COUNT(*)
                            FROM listings l
                            WHERE l.shop_id = s.id
                              AND l.deleted_at IS NULL
                              AND l.status = 'active'
                              AND l.available_slots > 0
                              AND NOT EXISTS (
                                    SELECT 1
                                    FROM assignments ax
                                    JOIN listings lx ON lx.id = ax.listing_id
                                    WHERE ax.buyer_user_id = %s
                                      AND lx.wb_product_id = l.wb_product_id
                                      AND ax.status = ANY (
                                            ARRAY[
                                                'reserved'::text,
                                                'order_verified'::text,
                                                'picked_up_wait_review'::text,
                                                'picked_up_wait_unlock'::text,
                                                'withdraw_sent'::text
                                            ]
                                      )
                              )
                              AND NOT EXISTS (
                                    SELECT 1
                                    FROM buyer_orders bo
                                    WHERE bo.buyer_user_id = %s
                                      AND bo.wb_product_id = l.wb_product_id
                              )
                        ) AS active_listings_count
                    FROM buyer_saved_shops bss
                    JOIN shops s ON s.id = bss.shop_id
                    WHERE bss.buyer_user_id = %s
                      AND s.deleted_at IS NULL
                    ORDER BY bss.last_opened_at DESC, s.id DESC
                    LIMIT %s
                    """,
                    (buyer_user_id, buyer_user_id, buyer_user_id, limit),
                )
                rows = await cur.fetchall()
                return [
                    BuyerSavedShopResult(
                        shop_id=row["id"],
                        slug=row["slug"],
                        title=row["title"],
                        last_opened_at=row["last_opened_at"],
                        active_listings_count=int(row["active_listings_count"]),
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def resolve_saved_shop_for_buyer(
        self,
        *,
        buyer_user_id: int,
        shop_id: int,
    ) -> BuyerShopResult:
        async def operation(conn: AsyncConnection) -> BuyerShopResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        s.id,
                        s.slug,
                        s.title
                    FROM buyer_saved_shops bss
                    JOIN shops s ON s.id = bss.shop_id
                    WHERE bss.buyer_user_id = %s
                      AND bss.shop_id = %s
                      AND s.deleted_at IS NULL
                    """,
                    (buyer_user_id, shop_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"saved shop {shop_id} not found for buyer {buyer_user_id}")
                return BuyerShopResult(
                    shop_id=row["id"],
                    slug=row["slug"],
                    title=row["title"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def remove_saved_shop(
        self,
        *,
        buyer_user_id: int,
        shop_id: int,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT 1
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    WHERE a.buyer_user_id = %s
                      AND l.shop_id = %s
                      AND a.status = ANY(%s)
                    LIMIT 1
                    """,
                    (
                        buyer_user_id,
                        shop_id,
                        ["reserved", "order_verified", "picked_up_wait_review", "picked_up_wait_unlock"],
                    ),
                )
                if await cur.fetchone() is not None:
                    raise InvalidStateError("saved shop cannot be removed while buyer has unfinished purchase there")

                await cur.execute(
                    """
                    DELETE FROM buyer_saved_shops
                    WHERE buyer_user_id = %s
                      AND shop_id = %s
                    """,
                    (buyer_user_id, shop_id),
                )
                return StatusChangeResult(changed=cur.rowcount > 0)

        return await run_in_transaction(self._pool, operation)

    async def reserve_listing_slot(
        self,
        *,
        buyer_user_id: int,
        listing_id: int,
        idempotency_key: str,
        reservation_timeout_hours: int = 4,
    ) -> AssignmentReservationResult:
        existing = await self._find_reservation_by_idempotency(idempotency_key=idempotency_key)
        if existing is not None:
            return existing

        await self._ensure_buyer_user_exists(user_id=buyer_user_id)
        seller_collateral_account_id = await self._get_seller_collateral_account_for_listing(
            listing_id=listing_id,
            buyer_user_id=buyer_user_id,
        )
        reward_reserved_account_id = await self._ensure_system_account_id(account_kind="reward_reserved")

        return await self._finance_service.create_assignment_reservation(
            listing_id=listing_id,
            buyer_user_id=buyer_user_id,
            seller_collateral_account_id=seller_collateral_account_id,
            reward_reserved_account_id=reward_reserved_account_id,
            idempotency_key=idempotency_key,
            reservation_timeout_hours=reservation_timeout_hours,
            review_required=True,
        )

    async def _find_reservation_by_idempotency(
        self,
        *,
        idempotency_key: str,
    ) -> AssignmentReservationResult | None:
        async def operation(conn: AsyncConnection) -> AssignmentReservationResult | None:
            async with conn.cursor(row_factory=dict_row) as cur:
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
                return AssignmentReservationResult(
                    assignment_id=row["id"],
                    created=False,
                    reward_usdt=row["reward_usdt"],
                    reservation_expires_at=row["reservation_expires_at"],
                    task_uuid=row["task_uuid"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def submit_purchase_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> BuyerOrderSubmitResult:
        decoded = decode_purchase_payload(payload_base64)

        async def operation(conn: AsyncConnection) -> BuyerOrderSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
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
                    (assignment_id,),
                )
                assignment = await cur.fetchone()
                if assignment is None:
                    raise NotFoundError(f"assignment {assignment_id} not found")
                if assignment["buyer_user_id"] != buyer_user_id:
                    raise NotFoundError(f"assignment {assignment_id} not found for buyer")
                if assignment["status"] not in _ASSIGNMENT_PAYLOAD_ALLOWED_STATES:
                    raise InvalidStateError("assignment cannot accept payload in current state")
                if decoded.task_uuid != assignment["task_uuid"]:
                    raise PayloadValidationError("payload field 'task_uuid' does not match assignment")
                if decoded.wb_product_id != assignment["wb_product_id"]:
                    raise PayloadValidationError("payload field 'wb_product_id' does not match assignment listing")

                await cur.execute("SELECT now() AS current_time")
                now_row = await cur.fetchone()
                current_time = now_row["current_time"]
                if assignment["status"] == "reserved" and assignment["reservation_expires_at"] <= current_time:
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
                    (assignment_id,),
                )
                existing_order = await cur.fetchone()
                if existing_order is not None:
                    if (
                        existing_order["order_id"] == decoded.order_id
                        and existing_order["ordered_at"] == decoded.ordered_at
                    ):
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
                            (decoded.order_id, assignment_id),
                        )
                        return BuyerOrderSubmitResult(
                            assignment_id=assignment_id,
                            changed=False,
                            status="order_verified",
                            order_id=decoded.order_id,
                            wb_product_id=assignment["wb_product_id"],
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
                if duplicate_order is not None and duplicate_order["assignment_id"] != assignment_id:
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
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'plugin_base64')
                        """,
                        (
                            assignment_id,
                            assignment["listing_id"],
                            buyer_user_id,
                            assignment["task_uuid"],
                            decoded.order_id,
                            assignment["wb_product_id"],
                            decoded.ordered_at,
                            decoded.payload_version,
                            Json(decoded.raw_payload_json),
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
                    (decoded.order_id, assignment_id),
                )
                await self._notifications.enqueue_assignment_order_verified_for_seller_locked(
                    cur,
                    assignment_id=assignment_id,
                )
                return BuyerOrderSubmitResult(
                    assignment_id=assignment_id,
                    changed=True,
                    status="order_verified",
                    order_id=decoded.order_id,
                    wb_product_id=assignment["wb_product_id"],
                    ordered_at=decoded.ordered_at,
                )

        return await run_in_transaction(self._pool, operation)

    async def submit_review_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> BuyerReviewSubmitResult:
        decoded = decode_review_payload(payload_base64)

        async def operation(conn: AsyncConnection) -> BuyerReviewSubmitResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                assignment = await self._load_review_assignment_locked(
                    cur,
                    assignment_id=assignment_id,
                )
                if assignment is None:
                    raise NotFoundError(f"assignment {assignment_id} not found")
                if assignment["buyer_user_id"] != buyer_user_id:
                    raise NotFoundError(f"assignment {assignment_id} not found for buyer")
                return await self._store_review_payload_locked(
                    cur,
                    assignment=assignment,
                    decoded=decoded,
                    source="plugin_base64",
                )

        return await run_in_transaction(self._pool, operation)

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> AdminReviewVerificationResult:
        decoded = decode_review_payload(payload_base64)

        async def operation(conn: AsyncConnection) -> AdminReviewVerificationResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                assignment = await self._load_review_assignment_locked(
                    cur,
                    assignment_id=assignment_id,
                )
                if assignment is None:
                    raise NotFoundError(f"assignment {assignment_id} not found")
                result = await self._store_review_payload_locked(
                    cur,
                    assignment=assignment,
                    decoded=decoded,
                    source="admin_base64",
                    admin_user_id=admin_user_id,
                    idempotency_key=idempotency_key,
                )
                return AdminReviewVerificationResult(
                    assignment_id=result.assignment_id,
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

    async def list_admin_pending_review_confirmations(
        self,
        *,
        limit: int = 20,
    ) -> list[AdminPendingReviewConfirmationView]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        async def operation(conn: AsyncConnection) -> list[AdminPendingReviewConfirmationView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id AS assignment_id,
                        a.task_uuid,
                        a.listing_id,
                        a.buyer_user_id,
                        a.review_phrases,
                        u.telegram_id AS buyer_telegram_id,
                        u.username AS buyer_username,
                        s.title AS shop_title,
                        COALESCE(l.display_title, l.search_phrase) AS display_title,
                        l.wb_product_id,
                        br.reviewed_at,
                        br.rating,
                        br.review_text,
                        br.verification_reason
                    FROM buyer_reviews br
                    JOIN assignments a ON a.id = br.assignment_id
                    JOIN users u ON u.id = a.buyer_user_id
                    JOIN listings l ON l.id = a.listing_id
                    JOIN shops s ON s.id = l.shop_id
                    WHERE a.status = 'picked_up_wait_review'
                      AND br.verification_status = %s
                    ORDER BY br.updated_at ASC, br.id ASC
                    LIMIT %s
                    """,
                    (_REVIEW_STATUS_PENDING_MANUAL, limit),
                )
                rows = await cur.fetchall()
                return [
                    AdminPendingReviewConfirmationView(
                        assignment_id=row["assignment_id"],
                        task_uuid=row["task_uuid"],
                        listing_id=row["listing_id"],
                        buyer_user_id=row["buyer_user_id"],
                        buyer_telegram_id=row["buyer_telegram_id"],
                        buyer_username=row["buyer_username"],
                        shop_title=row["shop_title"],
                        display_title=row["display_title"],
                        wb_product_id=row["wb_product_id"],
                        reviewed_at=row["reviewed_at"],
                        rating=int(row["rating"]),
                        review_text=row["review_text"],
                        review_phrases=list(row["review_phrases"] or []),
                        verification_reason=row["verification_reason"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def _load_review_assignment_locked(
        self,
        cur,
        *,
        assignment_id: int,
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
            WHERE a.id = %s
            FOR UPDATE OF a, l
            """,
            (assignment_id,),
        )
        return await cur.fetchone()

    async def _store_review_payload_locked(
        self,
        cur,
        *,
        assignment: dict[str, Any],
        decoded: DecodedReviewPayload,
        source: str,
        admin_user_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> BuyerReviewSubmitResult:
        if assignment["status"] not in {
            "picked_up_wait_review",
            "picked_up_wait_unlock",
            "withdraw_sent",
        }:
            raise InvalidStateError("assignment cannot accept review payload in current state")
        if decoded.task_uuid != assignment["task_uuid"]:
            raise PayloadValidationError("payload field 'task_uuid' does not match assignment")
        if decoded.wb_product_id != assignment["wb_product_id"]:
            raise PayloadValidationError("payload field 'wb_product_id' does not match assignment listing")

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
            (assignment["id"],),
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
            (
                target_verification_status,
                target_verification_reason,
            ) = _evaluate_review_verification(
                rating=decoded.rating,
                review_text=decoded.review_text,
                required_phrases=list(assignment["review_phrases"] or []),
            )

        if same_payload and (
            admin_user_id is None
            or assignment["status"] != "picked_up_wait_review"
            or existing_review["verification_status"] == _REVIEW_STATUS_VERIFIED_ADMIN
        ):
            return BuyerReviewSubmitResult(
                assignment_id=assignment["id"],
                changed=False,
                status=assignment["status"],
                task_uuid=assignment["task_uuid"],
                wb_product_id=assignment["wb_product_id"],
                reviewed_at=decoded.reviewed_at,
                rating=decoded.rating,
                review_text=decoded.review_text,
                verification_status=existing_review["verification_status"],
                verification_reason=existing_review["verification_reason"],
            )

        if existing_review is not None and assignment["status"] != "picked_up_wait_review":
            raise InvalidStateError("assignment review is already completed")
        if existing_review is None and assignment["status"] != "picked_up_wait_review":
            raise InvalidStateError("assignment review is already completed")

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
                        assignment["id"],
                        assignment["listing_id"],
                        assignment["buyer_user_id"],
                        assignment["task_uuid"],
                        assignment["wb_product_id"],
                        decoded.reviewed_at,
                        decoded.rating,
                        decoded.review_text,
                        target_verification_status,
                        target_verification_reason,
                        datetime.now(tz=UTC) if target_verification_status != _REVIEW_STATUS_PENDING_MANUAL else None,
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
                    assignment["task_uuid"],
                    decoded.reviewed_at,
                    decoded.rating,
                    decoded.review_text,
                    target_verification_status,
                    target_verification_reason,
                    datetime.now(tz=UTC) if target_verification_status != _REVIEW_STATUS_PENDING_MANUAL else None,
                    admin_user_id,
                    decoded.payload_version,
                    Json(decoded.raw_payload_json),
                    source,
                    assignment["id"],
                ),
            )

        assignment_status = assignment["status"]
        if target_verification_status != _REVIEW_STATUS_PENDING_MANUAL:
            await cur.execute(
                """
                UPDATE assignments
                SET status = 'picked_up_wait_unlock',
                    updated_at = timezone('utc', now())
                WHERE id = %s
                """,
                (assignment["id"],),
            )
            assignment_status = "picked_up_wait_unlock"
            if assignment["status"] == "picked_up_wait_review":
                await self._notifications.enqueue_assignment_review_confirmed_for_seller_locked(
                    cur,
                    assignment_id=assignment["id"],
                )
            if admin_user_id is not None and idempotency_key is not None:
                await self._finance_service.insert_admin_audit_locked(
                    cur,
                    admin_user_id=admin_user_id,
                    action="assignment_review_verified_admin",
                    target_type="assignment",
                    target_id=str(assignment["id"]),
                    payload={
                        "assignment_id": assignment["id"],
                        "task_uuid": str(assignment["task_uuid"]),
                        "wb_product_id": assignment["wb_product_id"],
                        "reviewed_at": decoded.reviewed_at.isoformat(),
                        "rating": decoded.rating,
                        "review_text": decoded.review_text,
                    },
                    idempotency_key=f"{idempotency_key}:audit",
                )

        return BuyerReviewSubmitResult(
            assignment_id=assignment["id"],
            changed=True,
            status=assignment_status,
            task_uuid=assignment["task_uuid"],
            wb_product_id=assignment["wb_product_id"],
            reviewed_at=decoded.reviewed_at,
            rating=decoded.rating,
            review_text=decoded.review_text,
            verification_status=target_verification_status,
            verification_reason=target_verification_reason,
        )

    async def _ensure_buyer_user_exists(self, *, user_id: int) -> None:
        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor(row_factory=dict_row) as cur:
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
                    (user_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"buyer user {user_id} not found")

        await run_in_transaction(self._pool, operation, read_only=True)

    async def list_buyer_assignments(self, *, buyer_user_id: int) -> list[BuyerAssignmentView]:
        async def operation(conn: AsyncConnection) -> list[BuyerAssignmentView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id,
                        a.listing_id,
                        a.task_uuid,
                        a.status,
                        a.reward_usdt,
                        a.reservation_expires_at,
                        a.order_id,
                        a.review_required,
                        a.review_phrases,
                        l.wb_product_id,
                        l.display_title,
                        l.wb_source_title,
                        l.reference_price_rub,
                        l.wb_subject_name,
                        l.wb_brand_name,
                        l.wb_description,
                        l.wb_photo_url,
                        l.wb_tech_sizes_json,
                        l.wb_characteristics_json,
                        l.search_phrase,
                        s.id AS shop_id,
                        s.slug AS shop_slug,
                        s.title AS shop_title,
                        bo.ordered_at,
                        br.verification_status AS review_verification_status,
                        br.verification_reason AS review_verification_reason
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    JOIN shops s ON s.id = l.shop_id
                    LEFT JOIN buyer_orders bo ON bo.assignment_id = a.id
                    LEFT JOIN buyer_reviews br ON br.assignment_id = a.id
                    WHERE a.buyer_user_id = %s
                    ORDER BY a.created_at DESC
                    """,
                    (buyer_user_id,),
                )
                rows = await cur.fetchall()
                return [
                    BuyerAssignmentView(
                        assignment_id=row["id"],
                        listing_id=row["listing_id"],
                        task_uuid=row["task_uuid"],
                        shop_id=row["shop_id"],
                        shop_slug=row["shop_slug"],
                        shop_title=row["shop_title"],
                        wb_product_id=row["wb_product_id"],
                        display_title=row["display_title"],
                        wb_source_title=row["wb_source_title"],
                        reference_price_rub=row["reference_price_rub"],
                        wb_subject_name=row["wb_subject_name"],
                        wb_brand_name=row["wb_brand_name"],
                        wb_description=row["wb_description"],
                        wb_photo_url=row["wb_photo_url"],
                        wb_tech_sizes=list(row["wb_tech_sizes_json"] or []),
                        wb_characteristics=list(row["wb_characteristics_json"] or []),
                        search_phrase=row["search_phrase"],
                        status=row["status"],
                        reward_usdt=row["reward_usdt"],
                        reservation_expires_at=row["reservation_expires_at"],
                        order_id=row["order_id"],
                        ordered_at=row["ordered_at"],
                        review_required=bool(row["review_required"]),
                        review_phrases=list(row["review_phrases"] or []),
                        review_verification_status=row["review_verification_status"],
                        review_verification_reason=row["review_verification_reason"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def cancel_assignment_by_buyer(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> dict[str, Any]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        a.id,
                        a.status,
                        a.buyer_user_id,
                        l.seller_user_id
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    WHERE a.id = %s
                    FOR UPDATE OF a
                    """,
                    (assignment_id,),
                )
                assignment = await cur.fetchone()
                if assignment is None or assignment["buyer_user_id"] != buyer_user_id:
                    raise NotFoundError(f"assignment {assignment_id} not found for buyer")

                if assignment["status"] in {_RESERVATION_EXPIRED_STATUS, _BUYER_CANCELLED_STATUS}:
                    return {"changed": False}
                if assignment["status"] != "reserved":
                    raise InvalidStateError("assignment cannot be cancelled in current state")

                await cur.execute(
                    """
                    SELECT id
                    FROM accounts
                    WHERE account_code = %s
                    """,
                    (f"user:{assignment['seller_user_id']}:seller_collateral",),
                )
                seller_account = await cur.fetchone()
                if seller_account is None:
                    raise NotFoundError("seller collateral account is missing")

                return {
                    "changed": True,
                    "seller_collateral_account_id": seller_account["id"],
                }

        cancellation = await run_in_transaction(self._pool, operation)
        if not cancellation["changed"]:
            return StatusChangeResult(changed=False)

        reward_reserved_account_id = await self._ensure_system_account_id(account_kind="reward_reserved")
        return await self._finance_service.cancel_assignment_reservation(
            assignment_id=assignment_id,
            new_status=_BUYER_CANCELLED_STATUS,
            seller_collateral_account_id=int(cancellation["seller_collateral_account_id"]),
            reward_reserved_account_id=reward_reserved_account_id,
            idempotency_key=idempotency_key,
        )

    async def process_expired_reservations(
        self,
        *,
        batch_size: int = 100,
    ) -> ReservationExpiryResult:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        async def operation(conn: AsyncConnection) -> list[dict[str, Any]]:
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
                    WHERE a.status = 'reserved'
                      AND a.reservation_expires_at <= timezone('utc', now())
                    ORDER BY a.reservation_expires_at ASC, a.id ASC
                    LIMIT %s
                    """,
                    (batch_size,),
                )
                rows = await cur.fetchall()
                return list(rows)

        candidates = await run_in_transaction(self._pool, operation, read_only=True)

        expired_count = 0
        for row in candidates:
            try:
                result = await self._finance_service.cancel_assignment_reservation(
                    assignment_id=row["assignment_id"],
                    new_status=_RESERVATION_EXPIRED_STATUS,
                    seller_collateral_account_id=row["seller_collateral_account_id"],
                    reward_reserved_account_id=row["reward_reserved_account_id"],
                    idempotency_key=(f"{_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX}:{row['assignment_id']}"),
                    notification_event="reservation_expired",
                )
                if result.changed:
                    expired_count += 1
            except (InvalidStateError, NotFoundError):
                continue

        return ReservationExpiryResult(
            processed_count=len(candidates),
            expired_count=expired_count,
        )

    async def _get_seller_collateral_account_for_listing(
        self,
        *,
        listing_id: int,
        buyer_user_id: int | None = None,
    ) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        l.id,
                        l.seller_user_id,
                        l.wb_product_id,
                        l.status,
                        l.deleted_at,
                        s.deleted_at AS shop_deleted_at
                    FROM listings l
                    JOIN shops s ON s.id = l.shop_id
                    WHERE l.id = %s
                    """,
                    (listing_id,),
                )
                listing = await cur.fetchone()
                if listing is None:
                    raise NotFoundError(f"listing {listing_id} not found")
                if listing["deleted_at"] is not None or listing["shop_deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")
                if listing["status"] != "active":
                    raise InvalidStateError("listing must be active for reservation")
                if buyer_user_id is not None:
                    await cur.execute(
                        """
                        SELECT 1
                        FROM buyer_orders
                        WHERE buyer_user_id = %s
                          AND wb_product_id = %s
                        LIMIT 1
                        """,
                        (buyer_user_id, listing["wb_product_id"]),
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
                          AND a.status = ANY (
                                ARRAY[
                                    'reserved'::text,
                                    'order_verified'::text,
                                    'picked_up_wait_review'::text,
                                    'picked_up_wait_unlock'::text,
                                    'withdraw_sent'::text
                                ]
                          )
                        LIMIT 1
                        """,
                        (buyer_user_id, listing["wb_product_id"]),
                    )
                    if await cur.fetchone() is not None:
                        raise InvalidStateError("buyer already has assignment for this item")

                return await self._ensure_owner_account(
                    cur,
                    owner_user_id=listing["seller_user_id"],
                    account_kind="seller_collateral",
                )

        return await run_in_transaction(self._pool, operation)

    async def _ensure_system_account_id(self, *, account_kind: str) -> int:
        return await self._finance_service.ensure_system_account_id(account_kind=account_kind)

    async def _ensure_owner_account(self, cur, *, owner_user_id: int, account_kind: str) -> int:
        return await self._finance_service.ensure_owner_account_locked(
            cur,
            owner_user_id=owner_user_id,
            account_kind=account_kind,
        )

    async def _ensure_system_account(self, cur, *, account_kind: str) -> int:
        return await self._finance_service.ensure_system_account_locked(
            cur,
            account_kind=account_kind,
        )


def decode_purchase_payload(payload_base64: str) -> DecodedPurchasePayload:
    normalized_payload = payload_base64.strip()
    if not normalized_payload:
        raise PayloadValidationError("payload must not be empty")

    try:
        payload_bytes = base64.b64decode(normalized_payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PayloadValidationError("payload must be valid base64") from exc

    try:
        payload_text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadValidationError("payload must be utf-8 encoded JSON") from exc

    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise PayloadValidationError("payload must be valid JSON array") from exc

    if not isinstance(parsed, list):
        raise PayloadValidationError("payload must be a JSON array")
    if len(parsed) != 5:
        raise PayloadValidationError(
            "payload must contain [token_type, task_uuid, wb_product_id, order_id, ordered_at]"
        )

    token_type = _require_positive_int(parsed[0], field_name="token_type")
    if token_type != _PURCHASE_TOKEN_TYPE:
        raise PayloadValidationError(f"payload field 'token_type' must be {_PURCHASE_TOKEN_TYPE}")

    task_uuid = _require_uuid(parsed[1], field_name="task_uuid")
    wb_product_id = _require_positive_int(parsed[2], field_name="wb_product_id")

    order_id_raw = parsed[3]
    if not isinstance(order_id_raw, str) or not order_id_raw.strip():
        raise PayloadValidationError("payload field 'order_id' must be non-empty string")
    order_id = order_id_raw.strip()

    ordered_at_raw = parsed[4]
    if not isinstance(ordered_at_raw, str):
        raise PayloadValidationError("payload field 'ordered_at' must be ISO datetime string")
    ordered_at = _parse_iso_datetime_utc(ordered_at_raw, field_name="ordered_at")

    return DecodedPurchasePayload(
        payload_version=_PURCHASE_PAYLOAD_VERSION,
        task_uuid=task_uuid,
        order_id=order_id,
        ordered_at=ordered_at,
        wb_product_id=wb_product_id,
        raw_payload_json=parsed,
    )


def decode_review_payload(payload_base64: str) -> DecodedReviewPayload:
    normalized_payload = payload_base64.strip()
    if not normalized_payload:
        raise PayloadValidationError("payload must not be empty")

    try:
        payload_bytes = base64.b64decode(normalized_payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PayloadValidationError("payload must be valid base64") from exc

    try:
        payload_text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadValidationError("payload must be utf-8 encoded JSON") from exc

    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise PayloadValidationError("payload must be valid JSON array") from exc

    if not isinstance(parsed, list):
        raise PayloadValidationError("payload must be a JSON array")
    if len(parsed) != 6:
        raise PayloadValidationError(
            "payload must contain [token_type, task_uuid, wb_product_id, reviewed_at, rating, review_text]"
        )

    token_type = _require_positive_int(parsed[0], field_name="token_type")
    if token_type != _REVIEW_TOKEN_TYPE:
        raise PayloadValidationError(f"payload field 'token_type' must be {_REVIEW_TOKEN_TYPE}")

    task_uuid = _require_uuid(parsed[1], field_name="task_uuid")
    wb_product_id = _require_positive_int(parsed[2], field_name="wb_product_id")

    reviewed_at_raw = parsed[3]
    if not isinstance(reviewed_at_raw, str):
        raise PayloadValidationError("payload field 'reviewed_at' must be ISO datetime string")
    reviewed_at = _parse_iso_datetime_utc(reviewed_at_raw, field_name="reviewed_at")

    rating = _require_positive_int(parsed[4], field_name="rating")
    if rating > 5:
        raise PayloadValidationError("payload field 'rating' must be between 1 and 5")

    review_text_raw = parsed[5]
    if not isinstance(review_text_raw, str) or not review_text_raw.strip():
        raise PayloadValidationError("payload field 'review_text' must be non-empty string")

    return DecodedReviewPayload(
        payload_version=_REVIEW_PAYLOAD_VERSION,
        task_uuid=task_uuid,
        wb_product_id=wb_product_id,
        reviewed_at=reviewed_at,
        rating=rating,
        review_text=review_text_raw.strip(),
        raw_payload_json=parsed,
    )


def _parse_iso_datetime_utc(value: str, *, field_name: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise PayloadValidationError(f"payload field '{field_name}' must not be empty")

    # Accept JS toISOString() payloads and normalize all timestamps to UTC.
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PayloadValidationError(f"payload field '{field_name}' is not valid ISO datetime") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _require_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer") from exc
    if normalized < 1:
        raise PayloadValidationError(f"payload field '{field_name}' must be positive integer")
    return normalized


def _require_uuid(value: Any, *, field_name: str) -> UUID:
    if not isinstance(value, str) or not value.strip():
        raise PayloadValidationError(f"payload field '{field_name}' must be UUID string")
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise PayloadValidationError(f"payload field '{field_name}' must be UUID string") from exc


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
