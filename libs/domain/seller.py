from __future__ import annotations

import re
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
    NotFoundError,
)
from libs.domain.models import (
    DeleteExecutionResult,
    DeletePreview,
    ListingResult,
    SellerBalanceSnapshot,
    SellerBootstrapResult,
    SellerListingCollateralView,
    ShopResult,
    StatusChangeResult,
    TokenInvalidationResult,
    TransferResult,
)

_OPEN_ASSIGNMENT_STATES = (
    "reserved",
    "order_submitted",
    "order_verified",
    "picked_up_wait_unlock",
    "eligible_for_withdrawal",
    "withdraw_pending_admin",
)
_MANUAL_SOURCE = "manual"
_SCRAPPER_WITHDRAWN_SOURCE = "scrapper_401_withdrawn"
_SCRAPPER_EXPIRED_SOURCE = "scrapper_401_token_expired"
_COLLATERAL_FEE_MULTIPLIER = Decimal("1.01")
_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


class SellerService:
    """Seller lifecycle operations implemented with plain SQL transactions."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def bootstrap_seller(
        self,
        *,
        telegram_id: int,
        username: str | None,
    ) -> SellerBootstrapResult:
        async def operation(conn: AsyncConnection) -> SellerBootstrapResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, role
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
                        INSERT INTO users (telegram_id, username, role)
                        VALUES (%s, %s, 'seller')
                        RETURNING id
                        """,
                        (telegram_id, username),
                    )
                    created = await cur.fetchone()
                    user_id = created["id"]
                    created_user = True
                else:
                    if existing["role"] not in {"seller", "admin"}:
                        raise InvalidStateError("telegram user already exists with non-seller role")
                    user_id = existing["id"]
                    if username is not None:
                        await cur.execute(
                            """
                            UPDATE users
                            SET username = %s,
                                updated_at = timezone('utc', now())
                            WHERE id = %s
                            """,
                            (username, user_id),
                        )

                seller_available_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=user_id,
                    account_kind="seller_available",
                )
                seller_collateral_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=user_id,
                    account_kind="seller_collateral",
                )

                return SellerBootstrapResult(
                    user_id=user_id,
                    created_user=created_user,
                    seller_available_account_id=seller_available_account_id,
                    seller_collateral_account_id=seller_collateral_account_id,
                )

        return await run_in_transaction(self._pool, operation)

    async def create_shop(
        self,
        *,
        seller_user_id: int,
        title: str,
        slug_hint: str | None = None,
    ) -> ShopResult:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("title must not be empty")

        async def operation(conn: AsyncConnection) -> ShopResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_seller_user(cur, seller_user_id)
                await self._ensure_shop_title_unique(
                    cur,
                    seller_user_id=seller_user_id,
                    title=normalized_title,
                )
                base_slug = _slugify(slug_hint or normalized_title)

                for attempt in range(1, 100):
                    slug = base_slug if attempt == 1 else f"{base_slug}-{attempt}"
                    try:
                        await cur.execute(
                            """
                            INSERT INTO shops (
                                seller_user_id,
                                slug,
                                title,
                                wb_token_status,
                                wb_token_status_source
                            )
                            VALUES (%s, %s, %s, 'unknown', %s)
                            RETURNING id, slug, title, deleted_at, wb_token_status
                            """,
                            (seller_user_id, slug, normalized_title, _MANUAL_SOURCE),
                        )
                    except UniqueViolation as exc:
                        constraint = exc.diag.constraint_name if exc.diag is not None else None
                        if constraint == "uq_shops_seller_title_active_ci":
                            raise InvalidStateError("shop title already exists") from exc
                        if constraint == "uq_shops_slug_active":
                            continue
                        raise

                    created = await cur.fetchone()
                    return ShopResult(
                        shop_id=created["id"],
                        slug=created["slug"],
                        title=created["title"],
                        deleted_at=created["deleted_at"],
                        wb_token_status=created["wb_token_status"],
                    )

                raise InvalidStateError("unable to allocate unique shop slug after 99 attempts")

        return await run_in_transaction(self._pool, operation)

    async def get_shop(self, *, seller_user_id: int, shop_id: int) -> ShopResult:
        async def operation(conn: AsyncConnection) -> ShopResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                shop = await self._fetch_shop_owned(
                    cur,
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    for_update=False,
                )
                return ShopResult(
                    shop_id=shop["id"],
                    slug=shop["slug"],
                    title=shop["title"],
                    deleted_at=shop["deleted_at"],
                    wb_token_status=shop["wb_token_status"],
                )

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def rename_shop(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        title: str,
    ) -> ShopResult:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("title must not be empty")

        async def operation(conn: AsyncConnection) -> ShopResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                shop = await self._fetch_shop_owned(
                    cur,
                    seller_user_id=seller_user_id,
                    shop_id=shop_id,
                    for_update=True,
                )
                if shop["title"].strip() == normalized_title:
                    return ShopResult(
                        shop_id=shop["id"],
                        slug=shop["slug"],
                        title=shop["title"],
                        deleted_at=shop["deleted_at"],
                        wb_token_status=shop["wb_token_status"],
                    )

                await self._ensure_shop_title_unique(
                    cur,
                    seller_user_id=seller_user_id,
                    title=normalized_title,
                    exclude_shop_id=shop_id,
                )

                base_slug = _slugify(normalized_title)
                for attempt in range(1, 100):
                    slug = base_slug if attempt == 1 else f"{base_slug}-{attempt}"
                    try:
                        await cur.execute(
                            """
                            UPDATE shops
                            SET title = %s,
                                slug = %s,
                                updated_at = timezone('utc', now())
                            WHERE id = %s
                            RETURNING id, slug, title, deleted_at, wb_token_status
                            """,
                            (normalized_title, slug, shop_id),
                        )
                    except UniqueViolation as exc:
                        constraint = exc.diag.constraint_name if exc.diag is not None else None
                        if constraint == "uq_shops_seller_title_active_ci":
                            raise InvalidStateError("shop title already exists") from exc
                        if constraint == "uq_shops_slug_active":
                            continue
                        raise

                    renamed = await cur.fetchone()
                    return ShopResult(
                        shop_id=renamed["id"],
                        slug=renamed["slug"],
                        title=renamed["title"],
                        deleted_at=renamed["deleted_at"],
                        wb_token_status=renamed["wb_token_status"],
                    )

                raise InvalidStateError("unable to allocate unique shop slug after 99 attempts")

        return await run_in_transaction(self._pool, operation)

    async def list_shops(
        self,
        *,
        seller_user_id: int,
        include_deleted: bool = False,
    ) -> list[ShopResult]:
        async def operation(conn: AsyncConnection) -> list[ShopResult]:
            async with conn.cursor(row_factory=dict_row) as cur:
                if include_deleted:
                    await cur.execute(
                        """
                        SELECT id, slug, title, deleted_at, wb_token_status
                        FROM shops
                        WHERE seller_user_id = %s
                        ORDER BY created_at ASC
                        """,
                        (seller_user_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT id, slug, title, deleted_at, wb_token_status
                        FROM shops
                        WHERE seller_user_id = %s
                          AND deleted_at IS NULL
                        ORDER BY created_at ASC
                        """,
                        (seller_user_id,),
                    )
                rows = await cur.fetchall()
                return [
                    ShopResult(
                        shop_id=row["id"],
                        slug=row["slug"],
                        title=row["title"],
                        deleted_at=row["deleted_at"],
                        wb_token_status=row["wb_token_status"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_shop_delete_preview(self, *, seller_user_id: int, shop_id: int) -> DeletePreview:
        async def operation(conn: AsyncConnection) -> DeletePreview:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_shop_owned(cur, seller_user_id=seller_user_id, shop_id=shop_id)
                return await self._load_shop_delete_preview(cur, shop_id=shop_id)

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def delete_shop(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        deleted_by_user_id: int,
        idempotency_key: str,
    ) -> DeleteExecutionResult:
        async def operation(conn: AsyncConnection) -> DeleteExecutionResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, deleted_at
                    FROM shops
                    WHERE id = %s
                      AND seller_user_id = %s
                    FOR UPDATE
                    """,
                    (shop_id, seller_user_id),
                )
                shop = await cur.fetchone()
                if shop is None:
                    raise NotFoundError(f"shop {shop_id} not found")
                if shop["deleted_at"] is not None:
                    return DeleteExecutionResult(
                        changed=False,
                        assignment_transfers_count=0,
                        assignment_transferred_usdt=Decimal("0.000000"),
                        unassigned_collateral_returned_usdt=Decimal("0.000000"),
                    )

                await self._ensure_seller_user(cur, seller_user_id)

                await cur.execute(
                    """
                    SELECT id
                    FROM listings
                    WHERE shop_id = %s
                      AND deleted_at IS NULL
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (shop_id,),
                )
                listings = await cur.fetchall()

                transferred_count = 0
                transferred_amount = Decimal("0.000000")
                returned_unassigned = Decimal("0.000000")
                for row in listings:
                    listing_result = await self._delete_listing_locked(
                        cur,
                        seller_user_id=seller_user_id,
                        listing_id=row["id"],
                        deleted_by_user_id=deleted_by_user_id,
                        idempotency_key=f"{idempotency_key}:listing:{row['id']}",
                    )
                    transferred_count += listing_result.assignment_transfers_count
                    transferred_amount += listing_result.assignment_transferred_usdt
                    returned_unassigned += listing_result.unassigned_collateral_returned_usdt

                await cur.execute(
                    """
                    UPDATE shops
                    SET deleted_at = timezone('utc', now()),
                        deleted_by_user_id = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (deleted_by_user_id, shop_id),
                )

                return DeleteExecutionResult(
                    changed=True,
                    assignment_transfers_count=transferred_count,
                    assignment_transferred_usdt=_normalize_amount(transferred_amount),
                    unassigned_collateral_returned_usdt=_normalize_amount(returned_unassigned),
                )

        return await run_in_transaction(self._pool, operation)

    async def save_validated_shop_token(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        token_ciphertext: str,
    ) -> StatusChangeResult:
        if not token_ciphertext:
            raise ValueError("token_ciphertext must not be empty")

        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE shops
                    SET wb_token_ciphertext = %s,
                        wb_token_status = 'valid',
                        wb_token_last_validated_at = timezone('utc', now()),
                        wb_token_last_error = NULL,
                        wb_token_status_source = %s,
                        wb_token_invalidated_at = NULL,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                      AND seller_user_id = %s
                      AND deleted_at IS NULL
                    RETURNING id
                    """,
                    (token_ciphertext, _MANUAL_SOURCE, shop_id, seller_user_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFoundError(f"shop {shop_id} not found")
                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def create_listing_draft(
        self,
        *,
        seller_user_id: int,
        shop_id: int,
        wb_product_id: int,
        search_phrase: str,
        reward_usdt: Decimal,
        slot_count: int,
    ) -> ListingResult:
        amount = _normalize_amount(reward_usdt)
        normalized_phrase = search_phrase.strip()
        if not normalized_phrase:
            raise ValueError("search_phrase must not be empty")
        if amount <= Decimal("0.000000"):
            raise ValueError("reward_usdt must be > 0")
        if slot_count < 1:
            raise ValueError("slot_count must be >= 1")

        collateral_required = _normalize_amount(
            amount * Decimal(slot_count) * _COLLATERAL_FEE_MULTIPLIER
        )

        async def operation(conn: AsyncConnection) -> ListingResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_shop_owned(cur, seller_user_id=seller_user_id, shop_id=shop_id)
                await cur.execute(
                    """
                    INSERT INTO listings (
                        shop_id,
                        seller_user_id,
                        wb_product_id,
                        search_phrase,
                        reward_usdt,
                        slot_count,
                        available_slots,
                        collateral_required_usdt,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                    RETURNING
                        id,
                        shop_id,
                        wb_product_id,
                        search_phrase,
                        status,
                        reward_usdt,
                        slot_count,
                        available_slots,
                        collateral_required_usdt,
                        deleted_at
                    """,
                    (
                        shop_id,
                        seller_user_id,
                        wb_product_id,
                        normalized_phrase,
                        amount,
                        slot_count,
                        slot_count,
                        collateral_required,
                    ),
                )
                row = await cur.fetchone()
                return ListingResult(
                    listing_id=row["id"],
                    shop_id=row["shop_id"],
                    wb_product_id=row["wb_product_id"],
                    search_phrase=row["search_phrase"],
                    status=row["status"],
                    reward_usdt=row["reward_usdt"],
                    slot_count=row["slot_count"],
                    available_slots=row["available_slots"],
                    collateral_required_usdt=row["collateral_required_usdt"],
                    deleted_at=row["deleted_at"],
                )

        return await run_in_transaction(self._pool, operation)

    async def list_listings(
        self,
        *,
        seller_user_id: int,
        shop_id: int | None = None,
        include_deleted: bool = False,
    ) -> list[ListingResult]:
        async def operation(conn: AsyncConnection) -> list[ListingResult]:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: list[Any] = [seller_user_id]
                query = """
                    SELECT
                        id,
                        shop_id,
                        wb_product_id,
                        search_phrase,
                        status,
                        reward_usdt,
                        slot_count,
                        available_slots,
                        collateral_required_usdt,
                        deleted_at
                    FROM listings
                    WHERE seller_user_id = %s
                """
                if shop_id is not None:
                    query += " AND shop_id = %s"
                    params.append(shop_id)
                if not include_deleted:
                    query += " AND deleted_at IS NULL"
                query += " ORDER BY created_at ASC"

                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()
                return [
                    ListingResult(
                        listing_id=row["id"],
                        shop_id=row["shop_id"],
                        wb_product_id=row["wb_product_id"],
                        search_phrase=row["search_phrase"],
                        status=row["status"],
                        reward_usdt=row["reward_usdt"],
                        slot_count=row["slot_count"],
                        available_slots=row["available_slots"],
                        collateral_required_usdt=row["collateral_required_usdt"],
                        deleted_at=row["deleted_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def get_seller_balance_snapshot(
        self,
        *,
        seller_user_id: int,
    ) -> SellerBalanceSnapshot:
        async def operation(conn: AsyncConnection) -> SellerBalanceSnapshot:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_seller_user(cur, seller_user_id)
                available_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=seller_user_id,
                    account_kind="seller_available",
                )
                collateral_account_id = await self._ensure_owner_account(
                    cur,
                    owner_user_id=seller_user_id,
                    account_kind="seller_collateral",
                )
                await cur.execute(
                    """
                    SELECT
                        id,
                        current_balance_usdt
                    FROM accounts
                    WHERE id = ANY(%s)
                    """,
                    ([available_account_id, collateral_account_id],),
                )
                rows = await cur.fetchall()
                by_id = {row["id"]: row["current_balance_usdt"] for row in rows}
                return SellerBalanceSnapshot(
                    seller_available_usdt=_normalize_amount(
                        by_id.get(available_account_id, Decimal("0.000000"))
                    ),
                    seller_collateral_usdt=_normalize_amount(
                        by_id.get(collateral_account_id, Decimal("0.000000"))
                    ),
                )

        return await run_in_transaction(self._pool, operation)

    async def list_listing_collateral_views(
        self,
        *,
        seller_user_id: int,
        shop_id: int | None = None,
        include_deleted: bool = False,
    ) -> list[SellerListingCollateralView]:
        async def operation(conn: AsyncConnection) -> list[SellerListingCollateralView]:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: list[Any] = [seller_user_id]
                query = """
                    SELECT
                        l.id,
                        l.shop_id,
                        l.wb_product_id,
                        l.search_phrase,
                        l.status,
                        l.reward_usdt,
                        l.slot_count,
                        l.available_slots,
                        l.collateral_required_usdt,
                        l.deleted_at,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN h.hold_type = 'collateral' AND h.status = 'active'
                                    THEN h.amount_usdt
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS collateral_locked_usdt,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN h.hold_type = 'slot_reserve' AND h.status = 'active'
                                    THEN h.amount_usdt
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS reserved_slot_usdt
                    FROM listings l
                    LEFT JOIN balance_holds h ON h.listing_id = l.id
                    WHERE l.seller_user_id = %s
                """
                if shop_id is not None:
                    query += " AND l.shop_id = %s"
                    params.append(shop_id)
                if not include_deleted:
                    query += " AND l.deleted_at IS NULL"
                query += """
                    GROUP BY
                        l.id,
                        l.shop_id,
                        l.wb_product_id,
                        l.search_phrase,
                        l.status,
                        l.reward_usdt,
                        l.slot_count,
                        l.available_slots,
                        l.collateral_required_usdt,
                        l.deleted_at,
                        l.created_at
                    ORDER BY l.created_at ASC, l.id ASC
                """

                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()
                return [
                    SellerListingCollateralView(
                        listing_id=row["id"],
                        shop_id=row["shop_id"],
                        wb_product_id=row["wb_product_id"],
                        search_phrase=row["search_phrase"],
                        status=row["status"],
                        reward_usdt=row["reward_usdt"],
                        slot_count=row["slot_count"],
                        available_slots=row["available_slots"],
                        collateral_required_usdt=row["collateral_required_usdt"],
                        collateral_locked_usdt=_normalize_amount(row["collateral_locked_usdt"]),
                        reserved_slot_usdt=_normalize_amount(row["reserved_slot_usdt"]),
                        deleted_at=row["deleted_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def activate_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
        idempotency_key: str,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        l.id,
                        l.status,
                        l.deleted_at,
                        l.collateral_required_usdt,
                        s.id AS shop_id,
                        s.wb_token_status,
                        s.wb_token_ciphertext
                    FROM listings l
                    JOIN shops s ON s.id = l.shop_id
                    WHERE l.id = %s
                      AND l.seller_user_id = %s
                    FOR UPDATE OF l, s
                    """,
                    (listing_id, seller_user_id),
                )
                listing = await cur.fetchone()
                if listing is None:
                    raise NotFoundError(f"listing {listing_id} not found")
                if listing["deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")
                if listing["status"] == "active":
                    return StatusChangeResult(changed=False)
                if listing["status"] != "draft":
                    raise InvalidStateError("listing can be activated only from draft state")
                if listing["wb_token_status"] != "valid" or not listing["wb_token_ciphertext"]:
                    raise InvalidStateError("shop token is not valid")

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

                amount = _normalize_amount(listing["collateral_required_usdt"])
                if amount > Decimal("0.000000"):
                    await self._transfer_locked(
                        cur,
                        from_account_id=seller_available_account_id,
                        to_account_id=seller_collateral_account_id,
                        amount_usdt=amount,
                        event_type="listing_activate_collateral_lock",
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

                await cur.execute(
                    """
                    UPDATE listings
                    SET status = 'active',
                        activated_at = COALESCE(activated_at, timezone('utc', now())),
                        paused_at = NULL,
                        pause_reason = NULL,
                        pause_source = NULL,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (listing_id,),
                )
                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def pause_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
        reason: str,
    ) -> StatusChangeResult:
        normalized_reason = reason.strip() or "manual_pause"

        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, status, deleted_at
                    FROM listings
                    WHERE id = %s
                      AND seller_user_id = %s
                    FOR UPDATE
                    """,
                    (listing_id, seller_user_id),
                )
                listing = await cur.fetchone()
                if listing is None:
                    raise NotFoundError(f"listing {listing_id} not found")
                if listing["deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")
                if listing["status"] == "paused":
                    return StatusChangeResult(changed=False)
                if listing["status"] != "active":
                    raise InvalidStateError("listing can be paused only from active state")

                await cur.execute(
                    """
                    UPDATE listings
                    SET status = 'paused',
                        paused_at = timezone('utc', now()),
                        pause_reason = %s,
                        pause_source = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (normalized_reason, _MANUAL_SOURCE, listing_id),
                )
                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def unpause_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
    ) -> StatusChangeResult:
        async def operation(conn: AsyncConnection) -> StatusChangeResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        l.id,
                        l.status,
                        l.deleted_at,
                        l.collateral_required_usdt,
                        s.wb_token_status,
                        s.wb_token_ciphertext
                    FROM listings l
                    JOIN shops s ON s.id = l.shop_id
                    WHERE l.id = %s
                      AND l.seller_user_id = %s
                    FOR UPDATE OF l, s
                    """,
                    (listing_id, seller_user_id),
                )
                listing = await cur.fetchone()
                if listing is None:
                    raise NotFoundError(f"listing {listing_id} not found")
                if listing["deleted_at"] is not None:
                    raise InvalidStateError("listing is deleted")
                if listing["status"] == "active":
                    return StatusChangeResult(changed=False)
                if listing["status"] != "paused":
                    raise InvalidStateError("listing can be unpaused only from paused state")
                if listing["wb_token_status"] != "valid" or not listing["wb_token_ciphertext"]:
                    raise InvalidStateError("shop token is not valid")

                await cur.execute(
                    """
                    SELECT COALESCE(SUM(amount_usdt), 0) AS collateral_sum
                    FROM balance_holds
                    WHERE listing_id = %s
                      AND hold_type = 'collateral'
                      AND status = 'active'
                    """,
                    (listing_id,),
                )
                row = await cur.fetchone()
                collateral_sum = _normalize_amount(row["collateral_sum"])
                required = _normalize_amount(listing["collateral_required_usdt"])
                if collateral_sum < required:
                    raise InvalidStateError("insufficient locked collateral for unpause")

                await cur.execute(
                    """
                    UPDATE listings
                    SET status = 'active',
                        paused_at = NULL,
                        pause_reason = NULL,
                        pause_source = NULL,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (listing_id,),
                )
                return StatusChangeResult(changed=True)

        return await run_in_transaction(self._pool, operation)

    async def get_listing_delete_preview(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
    ) -> DeletePreview:
        async def operation(conn: AsyncConnection) -> DeletePreview:
            async with conn.cursor(row_factory=dict_row) as cur:
                await self._ensure_listing_owned(
                    cur,
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                )
                return await self._load_listing_delete_preview(cur, listing_id=listing_id)

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def delete_listing(
        self,
        *,
        seller_user_id: int,
        listing_id: int,
        deleted_by_user_id: int,
        idempotency_key: str,
    ) -> DeleteExecutionResult:
        async def operation(conn: AsyncConnection) -> DeleteExecutionResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._delete_listing_locked(
                    cur,
                    seller_user_id=seller_user_id,
                    listing_id=listing_id,
                    deleted_by_user_id=deleted_by_user_id,
                    idempotency_key=idempotency_key,
                )

        return await run_in_transaction(self._pool, operation)

    async def invalidate_shop_token_and_pause(
        self,
        *,
        shop_id: int,
        source: str,
        error_message: str | None = None,
    ) -> TokenInvalidationResult:
        if source not in {_SCRAPPER_WITHDRAWN_SOURCE, _SCRAPPER_EXPIRED_SOURCE, _MANUAL_SOURCE}:
            raise ValueError("unsupported token invalidation source")

        new_status = "expired" if source == _SCRAPPER_EXPIRED_SOURCE else "invalid"

        async def operation(conn: AsyncConnection) -> TokenInvalidationResult:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, deleted_at
                    FROM shops
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (shop_id,),
                )
                shop = await cur.fetchone()
                if shop is None:
                    raise NotFoundError(f"shop {shop_id} not found")
                if shop["deleted_at"] is not None:
                    return TokenInvalidationResult(changed=False, paused_listings_count=0)

                await cur.execute(
                    """
                    UPDATE shops
                    SET wb_token_status = %s,
                        wb_token_last_error = %s,
                        wb_token_status_source = %s,
                        wb_token_invalidated_at = timezone('utc', now()),
                        wb_token_last_validated_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (new_status, error_message, source, shop_id),
                )
                shop_updated = cur.rowcount > 0

                await cur.execute(
                    """
                    UPDATE listings
                    SET status = 'paused',
                        paused_at = timezone('utc', now()),
                        pause_reason = %s,
                        pause_source = %s,
                        updated_at = timezone('utc', now())
                    WHERE shop_id = %s
                      AND deleted_at IS NULL
                      AND status = 'active'
                    """,
                    ("token_invalidated", source, shop_id),
                )
                paused_count = cur.rowcount

                return TokenInvalidationResult(
                    changed=shop_updated or paused_count > 0,
                    paused_listings_count=paused_count,
                )

        return await run_in_transaction(self._pool, operation)

    async def _delete_listing_locked(
        self,
        cur,
        *,
        seller_user_id: int,
        listing_id: int,
        deleted_by_user_id: int,
        idempotency_key: str,
    ) -> DeleteExecutionResult:
        await cur.execute(
            """
            SELECT id, deleted_at
            FROM listings
            WHERE id = %s
              AND seller_user_id = %s
            FOR UPDATE
            """,
            (listing_id, seller_user_id),
        )
        listing = await cur.fetchone()
        if listing is None:
            raise NotFoundError(f"listing {listing_id} not found")
        if listing["deleted_at"] is not None:
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
        reward_reserved_account_id = await self._ensure_system_account(
            cur,
            account_kind="reward_reserved",
        )

        await cur.execute(
            """
            SELECT h.id, h.assignment_id, h.amount_usdt, a.buyer_user_id
            FROM balance_holds h
            JOIN assignments a ON a.id = h.assignment_id
            WHERE h.listing_id = %s
              AND h.hold_type = 'slot_reserve'
              AND h.status = 'active'
            ORDER BY h.id ASC
            FOR UPDATE OF h, a
            """,
            (listing_id,),
        )
        active_slot_holds = await cur.fetchall()

        assignment_transfers_count = 0
        assignment_transferred_usdt = Decimal("0.000000")
        for hold in active_slot_holds:
            buyer_available_account_id = await self._ensure_owner_account(
                cur,
                owner_user_id=hold["buyer_user_id"],
                account_kind="buyer_available",
            )
            amount = _normalize_amount(hold["amount_usdt"])
            transfer_key = f"{idempotency_key}:assignment:{hold['assignment_id']}:hold:{hold['id']}"
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
                    "listing_id": listing_id,
                    "assignment_id": hold["assignment_id"],
                    "hold_id": hold["id"],
                },
            )
            if transfer_result.created:
                assignment_transfers_count += 1
                assignment_transferred_usdt += amount

            await cur.execute(
                """
                UPDATE assignments
                SET status = 'eligible_for_withdrawal',
                    updated_at = timezone('utc', now())
                WHERE id = %s
                  AND status <> 'eligible_for_withdrawal'
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
            (listing_id,),
        )
        collateral_holds = await cur.fetchall()
        collateral_sum = Decimal("0.000000")
        for hold in collateral_holds:
            collateral_sum += _normalize_amount(hold["amount_usdt"])

        unassigned_collateral = collateral_sum - assignment_transferred_usdt
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
                idempotency_key=_ledger_key(f"{idempotency_key}:collateral"),
                entity_type="listing",
                entity_id=listing_id,
                metadata={
                    "listing_id": listing_id,
                    "total_collateral": str(_normalize_amount(collateral_sum)),
                    "assignment_transferred_usdt": str(
                        _normalize_amount(assignment_transferred_usdt)
                    ),
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
            (_MANUAL_SOURCE, deleted_by_user_id, listing_id),
        )

        return DeleteExecutionResult(
            changed=True,
            assignment_transfers_count=assignment_transfers_count,
            assignment_transferred_usdt=_normalize_amount(assignment_transferred_usdt),
            unassigned_collateral_returned_usdt=unassigned_collateral,
        )

    async def _load_listing_delete_preview(self, cur, *, listing_id: int) -> DeletePreview:
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
            (listing_id, list(_OPEN_ASSIGNMENT_STATES), listing_id, listing_id),
        )
        row = await cur.fetchone()

        assignment_linked_reserved = _normalize_amount(row["assignment_linked_reserved_usdt"])
        collateral = _normalize_amount(row["collateral_usdt"])
        unassigned_collateral = collateral - assignment_linked_reserved
        if unassigned_collateral < Decimal("0.000000"):
            unassigned_collateral = Decimal("0.000000")

        return DeletePreview(
            active_listings_count=1,
            open_assignments_count=row["open_assignments_count"],
            assignment_linked_reserved_usdt=assignment_linked_reserved,
            unassigned_collateral_usdt=_normalize_amount(unassigned_collateral),
        )

    async def _load_shop_delete_preview(self, cur, *, shop_id: int) -> DeletePreview:
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
            (shop_id, shop_id, list(_OPEN_ASSIGNMENT_STATES), shop_id, shop_id),
        )
        row = await cur.fetchone()

        assignment_linked_reserved = _normalize_amount(row["assignment_linked_reserved_usdt"])
        collateral = _normalize_amount(row["collateral_usdt"])
        unassigned_collateral = collateral - assignment_linked_reserved
        if unassigned_collateral < Decimal("0.000000"):
            unassigned_collateral = Decimal("0.000000")

        return DeletePreview(
            active_listings_count=row["active_listings_count"],
            open_assignments_count=row["open_assignments_count"],
            assignment_linked_reserved_usdt=assignment_linked_reserved,
            unassigned_collateral_usdt=_normalize_amount(unassigned_collateral),
        )

    async def _ensure_seller_user(self, cur, user_id: int) -> None:
        await cur.execute(
            """
            SELECT id
            FROM users
            WHERE id = %s
              AND role IN ('seller', 'admin')
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"seller user {user_id} not found")

    async def _ensure_shop_owned(self, cur, *, seller_user_id: int, shop_id: int) -> None:
        await self._fetch_shop_owned(
            cur,
            seller_user_id=seller_user_id,
            shop_id=shop_id,
            for_update=False,
        )

    async def _fetch_shop_owned(
        self,
        cur,
        *,
        seller_user_id: int,
        shop_id: int,
        for_update: bool,
    ) -> dict[str, Any]:
        query = """
            SELECT id, slug, title, deleted_at, wb_token_status
            FROM shops
            WHERE id = %s
              AND seller_user_id = %s
              AND deleted_at IS NULL
        """
        if for_update:
            query += " FOR UPDATE"
        await cur.execute(query, (shop_id, seller_user_id))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"shop {shop_id} not found")
        return row

    async def _ensure_shop_title_unique(
        self,
        cur,
        *,
        seller_user_id: int,
        title: str,
        exclude_shop_id: int | None = None,
    ) -> None:
        params: list[Any] = [seller_user_id, title]
        query = """
            SELECT id
            FROM shops
            WHERE seller_user_id = %s
              AND deleted_at IS NULL
              AND lower(title) = lower(%s)
        """
        if exclude_shop_id is not None:
            query += " AND id <> %s"
            params.append(exclude_shop_id)
        query += " LIMIT 1"
        await cur.execute(query, tuple(params))
        existing = await cur.fetchone()
        if existing is not None:
            raise InvalidStateError("shop title already exists")

    async def _ensure_listing_owned(self, cur, *, seller_user_id: int, listing_id: int) -> None:
        await cur.execute(
            """
            SELECT id
            FROM listings
            WHERE id = %s
              AND seller_user_id = %s
              AND deleted_at IS NULL
            """,
            (listing_id, seller_user_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"listing {listing_id} not found")

    async def _ensure_owner_account(self, cur, *, owner_user_id: int, account_kind: str) -> int:
        account_code = f"user:{owner_user_id}:{account_kind}"
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
        account_code = f"system:{account_kind}"
        await cur.execute(
            """
            INSERT INTO accounts (
                owner_user_id,
                account_code,
                account_kind
            )
            VALUES (NULL, %s, %s)
            ON CONFLICT (account_code)
            DO UPDATE SET
                updated_at = timezone('utc', now())
            RETURNING id
            """,
            (account_code, account_kind),
        )
        row = await cur.fetchone()
        return row["id"]

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


def _slugify(raw: str) -> str:
    value = raw.strip().lower()
    value = "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in value)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value or "shop"


def _ledger_key(idempotency_key: str) -> str:
    return f"ledger:{idempotency_key}"


def _hold_key(idempotency_key: str) -> str:
    return f"hold:{idempotency_key}"


def _normalize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
