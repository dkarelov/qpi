from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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
    AssignmentReservationResult,
    BuyerAssignmentView,
    BuyerBootstrapResult,
    BuyerListingResult,
    BuyerOrderSubmitResult,
    BuyerShopResult,
    ReservationExpiryResult,
)

_ASSIGNMENT_PAYLOAD_ALLOWED_STATES = {"reserved", "order_submitted", "order_verified"}
_RESERVATION_EXPIRED_STATUS = "expired_2h"
_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX = "reservation-expire"


@dataclass(frozen=True)
class DecodedPurchasePayload:
    payload_version: int
    order_id: str
    wb_product_id: int
    ordered_at: datetime
    raw_payload_json: dict[str, Any]


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
                        VALUES (%s, %s, 'buyer')
                        RETURNING id
                        """,
                        (telegram_id, username),
                    )
                    created = await cur.fetchone()
                    user_id = created["id"]
                    created_user = True
                else:
                    if existing["role"] != "buyer":
                        raise InvalidStateError("telegram user already exists with non-buyer role")
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

    async def list_active_listings_by_shop_slug(self, *, slug: str) -> list[BuyerListingResult]:
        shop = await self.resolve_shop_by_slug(slug=slug)

        async def operation(conn: AsyncConnection) -> list[BuyerListingResult]:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        id,
                        shop_id,
                        wb_product_id,
                        discount_percent,
                        reward_usdt,
                        slot_count,
                        available_slots
                    FROM listings
                    WHERE shop_id = %s
                      AND deleted_at IS NULL
                      AND status = 'active'
                    ORDER BY created_at ASC
                    """,
                    (shop.shop_id,),
                )
                rows = await cur.fetchall()
                return [
                    BuyerListingResult(
                        listing_id=row["id"],
                        shop_id=row["shop_id"],
                        wb_product_id=row["wb_product_id"],
                        discount_percent=row["discount_percent"],
                        reward_usdt=row["reward_usdt"],
                        slot_count=row["slot_count"],
                        available_slots=row["available_slots"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

    async def reserve_listing_slot(
        self,
        *,
        buyer_user_id: int,
        listing_id: int,
        idempotency_key: str,
        reservation_timeout_hours: int = 2,
    ) -> AssignmentReservationResult:
        await self._ensure_buyer_user_exists(user_id=buyer_user_id)
        seller_collateral_account_id = await self._get_seller_collateral_account_for_listing(
            listing_id=listing_id
        )
        reward_reserved_account_id = await self._ensure_system_account_id(
            account_kind="reward_reserved"
        )

        return await self._finance_service.create_assignment_reservation(
            listing_id=listing_id,
            buyer_user_id=buyer_user_id,
            seller_collateral_account_id=seller_collateral_account_id,
            reward_reserved_account_id=reward_reserved_account_id,
            idempotency_key=idempotency_key,
            reservation_timeout_hours=reservation_timeout_hours,
        )

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

                await cur.execute("SELECT now() AS current_time")
                now_row = await cur.fetchone()
                current_time = now_row["current_time"]
                if (
                    assignment["status"] in {"reserved", "order_submitted"}
                    and assignment["reservation_expires_at"] <= current_time
                ):
                    raise InvalidStateError("reservation window has expired")

                if assignment["wb_product_id"] != decoded.wb_product_id:
                    raise PayloadValidationError(
                        "payload wb_product_id does not match listing product"
                    )

                await cur.execute(
                    """
                    SELECT
                        assignment_id,
                        order_id,
                        wb_product_id,
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
                        and existing_order["wb_product_id"] == decoded.wb_product_id
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
                            wb_product_id=decoded.wb_product_id,
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
                if (
                    duplicate_order is not None
                    and duplicate_order["assignment_id"] != assignment_id
                ):
                    raise DuplicateOrderError("order_id is already linked to another assignment")

                try:
                    await cur.execute(
                        """
                        INSERT INTO buyer_orders (
                            assignment_id,
                            listing_id,
                            buyer_user_id,
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
                            decoded.order_id,
                            decoded.wb_product_id,
                            decoded.ordered_at,
                            decoded.payload_version,
                            Json(decoded.raw_payload_json),
                        ),
                    )
                except UniqueViolation as exc:
                    constraint_name = exc.diag.constraint_name if exc.diag is not None else None
                    if constraint_name == "uq_buyer_orders_order_id":
                        raise DuplicateOrderError(
                            "order_id is already linked to another assignment"
                        ) from exc
                    if constraint_name == "uq_buyer_orders_assignment_id":
                        raise InvalidStateError(
                            "assignment already has submitted order payload"
                        ) from exc
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
                return BuyerOrderSubmitResult(
                    assignment_id=assignment_id,
                    changed=True,
                    status="order_verified",
                    order_id=decoded.order_id,
                    wb_product_id=decoded.wb_product_id,
                    ordered_at=decoded.ordered_at,
                )

        return await run_in_transaction(self._pool, operation)

    async def _ensure_buyer_user_exists(self, *, user_id: int) -> None:
        async def operation(conn: AsyncConnection) -> None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE id = %s
                      AND role = 'buyer'
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
                        a.status,
                        a.reward_usdt,
                        a.reservation_expires_at,
                        a.order_id,
                        l.wb_product_id,
                        s.slug AS shop_slug,
                        bo.ordered_at
                    FROM assignments a
                    JOIN listings l ON l.id = a.listing_id
                    JOIN shops s ON s.id = l.shop_id
                    LEFT JOIN buyer_orders bo ON bo.assignment_id = a.id
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
                        shop_slug=row["shop_slug"],
                        wb_product_id=row["wb_product_id"],
                        status=row["status"],
                        reward_usdt=row["reward_usdt"],
                        reservation_expires_at=row["reservation_expires_at"],
                        order_id=row["order_id"],
                        ordered_at=row["ordered_at"],
                    )
                    for row in rows
                ]

        return await run_in_transaction(self._pool, operation, read_only=True)

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
                    idempotency_key=(
                        f"{_RESERVATION_TIMEOUT_IDEMPOTENCY_PREFIX}:{row['assignment_id']}"
                    ),
                )
                if result.changed:
                    expired_count += 1
            except (InvalidStateError, NotFoundError):
                continue

        return ReservationExpiryResult(
            processed_count=len(candidates),
            expired_count=expired_count,
        )

    async def _get_seller_collateral_account_for_listing(self, *, listing_id: int) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        l.id,
                        l.seller_user_id,
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

                return await self._ensure_owner_account(
                    cur,
                    owner_user_id=listing["seller_user_id"],
                    account_kind="seller_collateral",
                )

        return await run_in_transaction(self._pool, operation)

    async def _ensure_system_account_id(self, *, account_kind: str) -> int:
        async def operation(conn: AsyncConnection) -> int:
            async with conn.cursor(row_factory=dict_row) as cur:
                return await self._ensure_system_account(cur, account_kind=account_kind)

        return await run_in_transaction(self._pool, operation)

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
            DO UPDATE SET updated_at = timezone('utc', now())
            RETURNING id
            """,
            (account_code, account_kind),
        )
        row = await cur.fetchone()
        return row["id"]


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
        raise PayloadValidationError("payload must be valid JSON object") from exc

    if not isinstance(parsed, dict):
        raise PayloadValidationError("payload must be a JSON object")

    missing_fields = [
        field_name
        for field_name in ("v", "order_id", "wb_product_id", "ordered_at")
        if field_name not in parsed
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise PayloadValidationError(f"payload is missing required fields: {missing}")

    version = parsed["v"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PayloadValidationError("payload field 'v' must be integer >= 1")

    order_id_raw = parsed["order_id"]
    if not isinstance(order_id_raw, str) or not order_id_raw.strip():
        raise PayloadValidationError("payload field 'order_id' must be non-empty string")
    order_id = order_id_raw.strip()

    wb_product_id = parsed["wb_product_id"]
    if not isinstance(wb_product_id, int) or isinstance(wb_product_id, bool) or wb_product_id < 1:
        raise PayloadValidationError("payload field 'wb_product_id' must be integer >= 1")

    ordered_at_raw = parsed["ordered_at"]
    if not isinstance(ordered_at_raw, str):
        raise PayloadValidationError("payload field 'ordered_at' must be RFC3339 UTC string")
    ordered_at = _parse_rfc3339_utc(ordered_at_raw)

    return DecodedPurchasePayload(
        payload_version=version,
        order_id=order_id,
        wb_product_id=wb_product_id,
        ordered_at=ordered_at,
        raw_payload_json=parsed,
    )


def _parse_rfc3339_utc(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise PayloadValidationError("payload field 'ordered_at' must not be empty")

    if normalized.endswith("Z"):
        iso_value = normalized[:-1] + "+00:00"
    elif normalized.endswith("+00:00"):
        iso_value = normalized
    else:
        raise PayloadValidationError("payload field 'ordered_at' must use UTC timezone")

    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise PayloadValidationError("payload field 'ordered_at' is not valid RFC3339") from exc

    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PayloadValidationError("payload field 'ordered_at' must use UTC timezone")

    return parsed.astimezone(UTC)
