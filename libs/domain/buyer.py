from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from libs.db.tx import run_in_transaction
from libs.domain.errors import (
    InvalidStateError,
    NotFoundError,
)
from libs.domain.ledger import FinanceService
from libs.domain.models import (
    AdminPendingReviewConfirmationView,
    AdminReviewVerificationResult,
    AssignmentReservationResult,
    BuyerAssignmentView,
    BuyerBootstrapResult,
    BuyerListingDeepLinkResult,
    BuyerListingResult,
    BuyerOrderSubmitResult,
    BuyerReviewSubmitResult,
    BuyerSavedShopResult,
    BuyerShopResult,
    ReservationExpiryResult,
    StatusChangeResult,
)
from libs.domain.notifications import NotificationService
from libs.domain.purchase_lifecycle import PurchaseLifecycleService

_ACTIVE_ASSIGNMENT_STATES = (
    "reserved",
    "order_verified",
    "picked_up_wait_review",
    "picked_up_wait_unlock",
    "withdraw_sent",
)
# Completed purchases are detected through buyer_orders, which is written before withdraw_sent.
_IN_PROGRESS_ASSIGNMENT_STATES = (
    "reserved",
    "order_verified",
    "picked_up_wait_review",
    "picked_up_wait_unlock",
)
_VISIBLE_COMPLETED_ASSIGNMENT_STATES = ("withdraw_sent",)
_REVIEW_STATUS_PENDING_MANUAL = "pending_manual"


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
        self._purchase_lifecycle = PurchaseLifecycleService(
            pool,
            finance_service=self._finance_service,
            notification_service=self._notifications,
        )

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

                buyer_available_account_id = await self._finance_service.ensure_owner_account_locked(
                    cur,
                    owner_user_id=user_id,
                    account_kind="buyer_available",
                )
                buyer_withdraw_pending_account_id = await self._finance_service.ensure_owner_account_locked(
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

    async def resolve_active_listing_deep_link(
        self,
        *,
        listing_id: int,
        buyer_user_id: int | None = None,
    ) -> BuyerListingDeepLinkResult:
        if listing_id < 1:
            raise ValueError("listing_id must be >= 1")

        async def operation(conn: AsyncConnection) -> BuyerListingDeepLinkResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
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
                        l.available_slots,
                        s.slug AS shop_slug,
                        s.title AS shop_title,
                        EXISTS (
                            SELECT 1
                            FROM assignments ax
                            JOIN listings lx ON lx.id = ax.listing_id
                            WHERE ax.buyer_user_id = %s
                              AND lx.wb_product_id = l.wb_product_id
                              AND ax.status = ANY (%s)
                        ) AS has_in_progress_purchase,
                        EXISTS (
                            SELECT 1
                            FROM buyer_orders bo
                            JOIN assignments ay ON ay.id = bo.assignment_id
                            WHERE bo.buyer_user_id = %s
                              AND bo.wb_product_id = l.wb_product_id
                              AND ay.status = ANY (%s)
                        ) AS has_visible_prior_order,
                        EXISTS (
                            SELECT 1
                            FROM buyer_orders bo
                            WHERE bo.buyer_user_id = %s
                              AND bo.wb_product_id = l.wb_product_id
                        ) AS has_prior_order
                    FROM listings l
                    JOIN shops s ON s.id = l.shop_id
                    WHERE l.id = %s
                      AND l.deleted_at IS NULL
                      AND l.status = 'active'
                      AND s.deleted_at IS NULL
                    """,
                    (
                        buyer_user_id,
                        list(_IN_PROGRESS_ASSIGNMENT_STATES),
                        buyer_user_id,
                        list(_VISIBLE_COMPLETED_ASSIGNMENT_STATES),
                        buyer_user_id,
                        listing_id,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"listing {listing_id} not found")
                buyer_action_state = None
                if row["has_in_progress_purchase"]:
                    buyer_action_state = "active_purchase"
                elif row["has_visible_prior_order"]:
                    buyer_action_state = "already_purchased"
                elif row["has_prior_order"]:
                    buyer_action_state = "already_purchased_hidden"
                if buyer_action_state is None and row["available_slots"] <= 0:
                    raise NotFoundError(f"listing {listing_id} not found")
                listing = BuyerListingResult(
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
                return BuyerListingDeepLinkResult(
                    shop_id=row["shop_id"],
                    shop_slug=row["shop_slug"],
                    shop_title=row["shop_title"],
                    listing=listing,
                    buyer_action_state=buyer_action_state,
                )

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
        return await self._purchase_lifecycle.reserve_purchase(
            buyer_user_id=buyer_user_id,
            announcement_id=listing_id,
            idempotency_seed=idempotency_key,
            reservation_timeout_hours=reservation_timeout_hours,
            review_required=True,
        )

    async def submit_purchase_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> BuyerOrderSubmitResult:
        return await self._purchase_lifecycle.submit_order_proof(
            buyer_user_id=buyer_user_id,
            purchase_id=assignment_id,
            token_payload=payload_base64,
        )

    async def submit_purchase_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> BuyerOrderSubmitResult:
        return await self._purchase_lifecycle.submit_order_proof_by_task_uuid(
            buyer_user_id=buyer_user_id,
            token_payload=payload_base64,
        )

    async def submit_review_payload(
        self,
        *,
        buyer_user_id: int,
        assignment_id: int,
        payload_base64: str,
    ) -> BuyerReviewSubmitResult:
        return await self._purchase_lifecycle.submit_review_confirmation(
            buyer_user_id=buyer_user_id,
            purchase_id=assignment_id,
            token_payload=payload_base64,
        )

    async def submit_review_payload_by_task_uuid(
        self,
        *,
        buyer_user_id: int,
        payload_base64: str,
    ) -> BuyerReviewSubmitResult:
        return await self._purchase_lifecycle.submit_review_confirmation_by_task_uuid(
            buyer_user_id=buyer_user_id,
            token_payload=payload_base64,
        )

    async def admin_verify_review_payload(
        self,
        *,
        admin_user_id: int,
        assignment_id: int,
        payload_base64: str,
        idempotency_key: str,
    ) -> AdminReviewVerificationResult:
        return await self._purchase_lifecycle.admin_verify_review_confirmation(
            admin_user_id=admin_user_id,
            purchase_id=assignment_id,
            token_payload=payload_base64,
            idempotency_seed=idempotency_key,
        )

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
        return await self._purchase_lifecycle.cancel_reserved_purchase_by_buyer(
            buyer_user_id=buyer_user_id,
            purchase_id=assignment_id,
            idempotency_seed=idempotency_key,
        )

    async def process_expired_reservations(
        self,
        *,
        batch_size: int = 100,
    ) -> ReservationExpiryResult:
        return await self._purchase_lifecycle.process_expired_reservations(batch_size=batch_size)
