from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from psycopg.rows import dict_row

from libs.domain.buyer import BuyerService, decode_purchase_payload
from libs.domain.errors import DuplicateOrderError, InvalidStateError, PayloadValidationError
from services.bot_api.buyer_handlers import BuyerCommandProcessor
from tests.helpers import create_account, create_listing, create_shop, create_user


def _encode_payload(
    *,
    order_id: str,
    ordered_at: str = "2026-02-26T12:00:00",
    wb_product_id: int | None = None,
) -> str:
    payload: list[Any] = [order_id, ordered_at]
    if wb_product_id is not None:
        payload.append(wb_product_id)
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


async def _set_assignment_expired(db_pool, *, assignment_id: int) -> None:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assignments
                    SET reservation_expires_at = timezone('utc', now()) - interval '1 minute',
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (assignment_id,),
                )


async def _prepare_reservable_listing(
    db_pool,
    *,
    slug: str,
    wb_product_id: int,
    reward_usdt: Decimal,
    slot_count: int,
    available_slots: int,
) -> dict[str, Any]:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=810000 + wb_product_id,
                role="seller",
                username=f"seller_{wb_product_id}",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug=slug,
                title=f"Shop {slug}",
            )
            listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=wb_product_id,
                reward_usdt=reward_usdt,
                slot_count=slot_count,
                available_slots=available_slots,
                status="active",
            )
            seller_collateral_account_id = await create_account(
                conn,
                owner_user_id=seller_user_id,
                account_code=f"user:{seller_user_id}:seller_collateral",
                account_kind="seller_collateral",
                balance=reward_usdt * slot_count,
            )
            reward_reserved_account_id = await create_account(
                conn,
                owner_user_id=None,
                account_code="system:reward_reserved",
                account_kind="reward_reserved",
                balance=Decimal("0.000000"),
            )

    return {
        "seller_user_id": seller_user_id,
        "shop_id": shop_id,
        "listing_id": listing_id,
        "seller_collateral_account_id": seller_collateral_account_id,
        "reward_reserved_account_id": reward_reserved_account_id,
    }


@pytest.mark.asyncio
async def test_shop_deeplink_resolution_and_listing_visibility(db_pool) -> None:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=820001,
                role="seller",
                username="seller_catalog",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="catalog-shop",
                title="Catalog Shop",
            )
            active_listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=5001,
                display_title="Бумага для принтера",
                wb_source_title="BRAUBERG Бумага для принтера",
                wb_brand_name="BRAUBERG",
                reference_price_rub=400,
                reward_usdt=Decimal("4.000000"),
                slot_count=3,
                available_slots=3,
                status="active",
            )
            await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=5002,
                reward_usdt=Decimal("5.000000"),
                slot_count=2,
                available_slots=2,
                status="paused",
            )
            deleted_listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=5003,
                reward_usdt=Decimal("6.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE listings
                    SET deleted_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (deleted_listing_id,),
                )

    shop = await buyer_service.resolve_shop_by_slug(slug="catalog-shop")
    assert shop.slug == "catalog-shop"

    listings = await buyer_service.list_active_listings_by_shop_slug(slug="catalog-shop")
    assert [item.listing_id for item in listings] == [active_listing_id]
    assert listings[0].available_slots == 3
    assert listings[0].display_title == "Бумага для принтера"
    assert listings[0].reference_price_rub == 400


@pytest.mark.asyncio
async def test_shop_catalog_hides_active_listings_without_free_slots(db_pool) -> None:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=820011,
                role="seller",
                username="seller_catalog_slots",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="catalog-slots-shop",
                title="Catalog Slots Shop",
            )
            visible_listing_id = await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=5011,
                reward_usdt=Decimal("1.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )
            await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=5012,
                reward_usdt=Decimal("1.000000"),
                slot_count=1,
                available_slots=0,
                status="active",
            )

    listings = await buyer_service.list_active_listings_by_shop_slug(slug="catalog-slots-shop")
    assert [item.listing_id for item in listings] == [visible_listing_id]
    assert listings[0].wb_product_id == 5011


@pytest.mark.asyncio
async def test_saved_shops_are_persistent_and_ordered_by_last_opened(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    buyer = await buyer_service.bootstrap_buyer(telegram_id=820500, username="buyer_saved_shops")

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=820501,
                role="seller",
                username="seller_saved_shops",
            )
            shop_one_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="saved-shop-one",
                title="Saved Shop One",
            )
            shop_two_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="saved-shop-two",
                title="Saved Shop Two",
            )

    await buyer_service.touch_saved_shop(
        buyer_user_id=buyer.user_id,
        shop_id=shop_one_id,
    )
    await buyer_service.touch_saved_shop(
        buyer_user_id=buyer.user_id,
        shop_id=shop_two_id,
    )
    await buyer_service.touch_saved_shop(
        buyer_user_id=buyer.user_id,
        shop_id=shop_one_id,
    )

    saved_shops = await buyer_service.list_saved_shops(buyer_user_id=buyer.user_id, limit=10)
    assert [item.shop_id for item in saved_shops] == [shop_one_id, shop_two_id]
    assert [item.slug for item in saved_shops] == ["saved-shop-one", "saved-shop-two"]

    resolved = await buyer_service.resolve_saved_shop_for_buyer(
        buyer_user_id=buyer.user_id,
        shop_id=shop_two_id,
    )
    assert resolved.slug == "saved-shop-two"


@pytest.mark.asyncio
async def test_saved_shop_removal_is_blocked_while_buyer_has_unfinished_purchase(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="saved-shop-blocked",
        wb_product_id=7017,
        reward_usdt=Decimal("2.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(telegram_id=820550, username="buyer_saved_blocked")

    await buyer_service.touch_saved_shop(
        buyer_user_id=buyer.user_id,
        shop_id=fixture["shop_id"],
    )
    await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:saved-shop-blocked",
    )

    with pytest.raises(InvalidStateError, match="unfinished purchase"):
        await buyer_service.remove_saved_shop(
            buyer_user_id=buyer.user_id,
            shop_id=fixture["shop_id"],
        )


@pytest.mark.asyncio
async def test_saved_shops_hide_deleted_shops(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    buyer = await buyer_service.bootstrap_buyer(telegram_id=820600, username="buyer_saved_deleted")

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=820601,
                role="seller",
                username="seller_saved_deleted",
            )
            shop_id = await create_shop(
                conn,
                seller_user_id=seller_user_id,
                slug="saved-shop-deleted",
                title="Saved Shop Deleted",
            )

    await buyer_service.touch_saved_shop(
        buyer_user_id=buyer.user_id,
        shop_id=shop_id,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE shops
                    SET deleted_at = timezone('utc', now()),
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (shop_id,),
                )

    saved_shops = await buyer_service.list_saved_shops(buyer_user_id=buyer.user_id, limit=10)
    assert saved_shops == []


@pytest.mark.asyncio
async def test_catalog_hides_products_already_purchased_by_buyer(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="repeat-filter-shop",
        wb_product_id=7001,
        reward_usdt=Decimal("3.000000"),
        slot_count=2,
        available_slots=2,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            second_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=7002,
                reward_usdt=Decimal("2.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    buyer = await buyer_service.bootstrap_buyer(telegram_id=870001, username="buyer_repeat_filter")
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:870001:7001",
    )
    await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        payload_base64=_encode_payload(order_id="repeat-filter-order"),
    )

    listings = await buyer_service.list_active_listings_by_shop_slug(
        slug="repeat-filter-shop",
        buyer_user_id=buyer.user_id,
    )
    assert [item.listing_id for item in listings] == [second_listing_id]
    assert [item.wb_product_id for item in listings] == [7002]


@pytest.mark.asyncio
async def test_catalog_hides_products_with_active_assignment_for_buyer(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="active-filter-shop",
        wb_product_id=7051,
        reward_usdt=Decimal("3.000000"),
        slot_count=2,
        available_slots=2,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            second_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=7052,
                reward_usdt=Decimal("2.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    buyer = await buyer_service.bootstrap_buyer(telegram_id=870011, username="buyer_active_filter")
    await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:870011:7051",
    )

    listings = await buyer_service.list_active_listings_by_shop_slug(
        slug="active-filter-shop",
        buyer_user_id=buyer.user_id,
    )
    assert [item.listing_id for item in listings] == [second_listing_id]
    assert [item.wb_product_id for item in listings] == [7052]


@pytest.mark.asyncio
async def test_reserve_rejects_product_already_purchased_by_buyer(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="repeat-reserve-shop",
        wb_product_id=7101,
        reward_usdt=Decimal("4.000000"),
        slot_count=2,
        available_slots=2,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            duplicate_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=7101,
                reward_usdt=Decimal("4.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    buyer = await buyer_service.bootstrap_buyer(telegram_id=870002, username="buyer_repeat_reserve")
    first_reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:870002:7101:first",
    )
    await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer.user_id,
        assignment_id=first_reservation.assignment_id,
        payload_base64=_encode_payload(order_id="repeat-reserve-order"),
    )

    with pytest.raises(InvalidStateError, match="already purchased"):
        await buyer_service.reserve_listing_slot(
            buyer_user_id=buyer.user_id,
            listing_id=duplicate_listing_id,
            idempotency_key="reserve:buyer:870002:7101:second",
        )


@pytest.mark.asyncio
async def test_reserve_rejects_product_with_existing_active_assignment(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="repeat-active-reserve-shop",
        wb_product_id=7151,
        reward_usdt=Decimal("4.000000"),
        slot_count=3,
        available_slots=3,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            duplicate_listing_id = await create_listing(
                conn,
                shop_id=fixture["shop_id"],
                seller_user_id=fixture["seller_user_id"],
                wb_product_id=7151,
                reward_usdt=Decimal("4.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    buyer = await buyer_service.bootstrap_buyer(telegram_id=870012, username="buyer_repeat_active")
    await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:870012:7151:first",
    )

    with pytest.raises(InvalidStateError, match="already has assignment"):
        await buyer_service.reserve_listing_slot(
            buyer_user_id=buyer.user_id,
            listing_id=duplicate_listing_id,
            idempotency_key="reserve:buyer:870012:7151:second",
        )


@pytest.mark.asyncio
async def test_admin_can_bootstrap_buyer_and_operate_buyer_flow(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="admin-buyer-shop",
        wb_product_id=5099,
        reward_usdt=Decimal("3.000000"),
        slot_count=1,
        available_slots=1,
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            admin_user_id = await create_user(
                conn,
                telegram_id=839900,
                role="admin",
                username="admin_buyer",
            )

    bootstrap = await buyer_service.bootstrap_buyer(telegram_id=839900, username="admin_buyer")
    assert bootstrap.created_user is False
    assert bootstrap.user_id == admin_user_id

    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=bootstrap.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:admin-buyer:1",
    )
    assert reservation.created is True

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT role FROM users WHERE id = %s", (admin_user_id,))
            row = await cur.fetchone()
            assert row["role"] == "admin"


@pytest.mark.asyncio
async def test_seller_can_later_bootstrap_buyer_on_same_telegram_id(db_pool) -> None:
    buyer_service = BuyerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=839901,
                role="seller",
                username="seller_then_buyer",
            )

    bootstrap = await buyer_service.bootstrap_buyer(
        telegram_id=839901,
        username="seller_then_buyer",
    )

    assert bootstrap.user_id == seller_user_id
    assert bootstrap.created_user is False

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT role, is_seller, is_buyer, is_admin FROM users WHERE id = %s",
                (seller_user_id,),
            )
            row = await cur.fetchone()
            assert row["role"] == "seller"
            assert row["is_seller"] is True
            assert row["is_buyer"] is True
            assert row["is_admin"] is False


@pytest.mark.asyncio
async def test_reservation_is_idempotent_and_decrements_slot_once(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="reserve-shop",
        wb_product_id=5101,
        reward_usdt=Decimal("10.000000"),
        slot_count=2,
        available_slots=2,
    )

    buyer = await buyer_service.bootstrap_buyer(telegram_id=830001, username="buyer_reserve")

    first = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:830001:5101",
    )
    second = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:830001:5101",
    )

    assert first.created is True
    assert second.created is False
    assert first.assignment_id == second.assignment_id

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT available_slots FROM listings WHERE id = %s",
                (fixture["listing_id"],),
            )
            listing = await cur.fetchone()
            assert listing["available_slots"] == 1

            await cur.execute(
                """
                SELECT
                    ROUND(EXTRACT(EPOCH FROM (reservation_expires_at - created_at)))
                    AS timeout_seconds
                FROM assignments
                WHERE id = %s
                """,
                (first.assignment_id,),
            )
            timeout_row = await cur.fetchone()
            assert int(timeout_row["timeout_seconds"]) == 4 * 60 * 60

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["seller_collateral_account_id"],),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("10.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["reward_reserved_account_id"],),
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("10.000000")


@pytest.mark.asyncio
async def test_buyer_can_cancel_reserved_assignment_and_release_funds(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="cancel-shop",
        wb_product_id=5151,
        reward_usdt=Decimal("5.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(telegram_id=835151, username="buyer_cancel")
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:835151:5151",
    )

    first_cancel = await buyer_service.cancel_assignment_by_buyer(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        idempotency_key="cancel:buyer:835151:5151:first",
    )
    second_cancel = await buyer_service.cancel_assignment_by_buyer(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        idempotency_key="cancel:buyer:835151:5151:second",
    )

    assert first_cancel.changed is True
    assert second_cancel.changed is False

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (reservation.assignment_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "buyer_cancelled"

            await cur.execute(
                "SELECT available_slots FROM listings WHERE id = %s",
                (fixture["listing_id"],),
            )
            listing = await cur.fetchone()
            assert listing["available_slots"] == 1

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["seller_collateral_account_id"],),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("5.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["reward_reserved_account_id"],),
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("0.000000")


@pytest.mark.asyncio
async def test_worker_expiry_transitions_reserved_to_expired_and_releases_funds(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="expiry-shop",
        wb_product_id=5201,
        reward_usdt=Decimal("7.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(telegram_id=840001, username="buyer_expiry")

    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:840001:5201",
    )
    await _set_assignment_expired(db_pool, assignment_id=reservation.assignment_id)

    expiry_result = await buyer_service.process_expired_reservations(batch_size=20)
    assert expiry_result.processed_count >= 1
    assert expiry_result.expired_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (reservation.assignment_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "expired_2h"

            await cur.execute(
                "SELECT available_slots FROM listings WHERE id = %s",
                (fixture["listing_id"],),
            )
            listing = await cur.fetchone()
            assert listing["available_slots"] == 1

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["seller_collateral_account_id"],),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("7.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (fixture["reward_reserved_account_id"],),
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("0.000000")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_builder", "error_substring"),
    [
        (lambda _wb_product_id: "%%%not-base64%%%", "base64"),
        (
            lambda _wb_product_id: base64.b64encode(b"{not-json").decode("ascii"),
            "JSON",
        ),
        (
            lambda _wb_product_id: base64.b64encode(
                json.dumps({"order_id": "ORD-MISSING", "ordered_at": "2026-02-26T12:00:00"}).encode(
                    "utf-8"
                )
            ).decode("ascii"),
            "JSON array",
        ),
        (
            lambda _wb_product_id: base64.b64encode(
                json.dumps(["ORD-MISSING"]).encode("utf-8")
            ).decode("ascii"),
            "contain [order_id, ordered_at]",
        ),
    ],
)
async def test_submit_payload_validation_matrix_rejects_invalid_inputs(
    db_pool,
    payload_builder,
    error_substring: str,
) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="payload-shop",
        wb_product_id=5301,
        reward_usdt=Decimal("8.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(telegram_id=850001, username="buyer_payload")

    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key=f"reserve:buyer:850001:{error_substring}",
    )

    with pytest.raises(PayloadValidationError) as exc_info:
        await buyer_service.submit_purchase_payload(
            buyer_user_id=buyer.user_id,
            assignment_id=reservation.assignment_id,
            payload_base64=payload_builder(5301),
        )
    assert error_substring in str(exc_info.value)

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, order_id FROM assignments WHERE id = %s",
                (reservation.assignment_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "reserved"
            assert assignment["order_id"] is None

            await cur.execute(
                "SELECT COUNT(*) AS count FROM buyer_orders WHERE assignment_id = %s",
                (reservation.assignment_id,),
            )
            order_count = await cur.fetchone()
            assert order_count["count"] == 0


def test_decode_purchase_payload_accepts_js_utc_timestamp() -> None:
    decoded = decode_purchase_payload(
        _encode_payload(
            order_id="ORD-UTC-Z",
            ordered_at="2026-03-10T20:32:23.807Z",
        )
    )

    assert decoded.order_id == "ORD-UTC-Z"
    assert decoded.ordered_at == datetime(2026, 3, 10, 20, 32, 23, 807000, tzinfo=UTC)


def test_decode_purchase_payload_normalizes_timezone_offset_to_utc() -> None:
    decoded = decode_purchase_payload(
        _encode_payload(
            order_id="ORD-OFFSET",
            ordered_at="2026-02-26T15:00:00+03:00",
        )
    )

    assert decoded.order_id == "ORD-OFFSET"
    assert decoded.ordered_at == datetime(2026, 2, 26, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_submit_payload_rejects_wb_product_mismatch(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="payload-mismatch-shop",
        wb_product_id=5311,
        reward_usdt=Decimal("8.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(
        telegram_id=850011,
        username="buyer_payload_mismatch",
    )
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:buyer:850011:mismatch",
    )

    with pytest.raises(PayloadValidationError, match="wb_product_id"):
        await buyer_service.submit_purchase_payload(
            buyer_user_id=buyer.user_id,
            assignment_id=reservation.assignment_id,
            payload_base64=_encode_payload(
                order_id="ORD-PRODUCT-MISMATCH",
                wb_product_id=999999,
            ),
        )


@pytest.mark.asyncio
async def test_duplicate_order_id_is_rejected_for_second_assignment(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="dup-shop",
        wb_product_id=5401,
        reward_usdt=Decimal("9.000000"),
        slot_count=2,
        available_slots=2,
    )
    buyer_one = await buyer_service.bootstrap_buyer(telegram_id=860001, username="buyer_dup_1")
    buyer_two = await buyer_service.bootstrap_buyer(telegram_id=860002, username="buyer_dup_2")

    reservation_one = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer_one.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:dup:1",
    )
    reservation_two = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer_two.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:dup:2",
    )

    payload = _encode_payload(order_id="ORD-DUP")
    first_submit = await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer_one.user_id,
        assignment_id=reservation_one.assignment_id,
        payload_base64=payload,
    )
    assert first_submit.changed is True

    with pytest.raises(DuplicateOrderError):
        await buyer_service.submit_purchase_payload(
            buyer_user_id=buyer_two.user_id,
            assignment_id=reservation_two.assignment_id,
            payload_base64=payload,
        )

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM assignments WHERE id = %s",
                (reservation_two.assignment_id,),
            )
            assignment_two = await cur.fetchone()
            assert assignment_two["status"] == "reserved"


@pytest.mark.asyncio
async def test_valid_payload_moves_assignment_to_order_verified_and_is_idempotent(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="success-shop",
        wb_product_id=5501,
        reward_usdt=Decimal("11.000000"),
        slot_count=1,
        available_slots=1,
    )
    buyer = await buyer_service.bootstrap_buyer(telegram_id=870001, username="buyer_success")
    reservation = await buyer_service.reserve_listing_slot(
        buyer_user_id=buyer.user_id,
        listing_id=fixture["listing_id"],
        idempotency_key="reserve:success:1",
    )
    payload = _encode_payload(order_id="ORD-SUCCESS")

    first = await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        payload_base64=payload,
    )
    second = await buyer_service.submit_purchase_payload(
        buyer_user_id=buyer.user_id,
        assignment_id=reservation.assignment_id,
        payload_base64=payload,
    )

    assert first.changed is True
    assert second.changed is False
    assert first.status == "order_verified"

    assignments = await buyer_service.list_buyer_assignments(buyer_user_id=buyer.user_id)
    assert len(assignments) == 1
    assert assignments[0].status == "order_verified"
    assert assignments[0].order_id == "ORD-SUCCESS"

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, order_id FROM assignments WHERE id = %s",
                (reservation.assignment_id,),
            )
            assignment = await cur.fetchone()
            assert assignment["status"] == "order_verified"
            assert assignment["order_id"] == "ORD-SUCCESS"

            await cur.execute(
                """
                SELECT
                    order_id,
                    wb_product_id,
                    payload_version,
                    raw_payload_json
                FROM buyer_orders
                WHERE assignment_id = %s
                """,
                (reservation.assignment_id,),
            )
            order = await cur.fetchone()
            assert order["order_id"] == "ORD-SUCCESS"
            assert order["wb_product_id"] == 5501
            assert order["payload_version"] == 2
            assert order["raw_payload_json"][0] == "ORD-SUCCESS"


@pytest.mark.asyncio
async def test_buyer_command_processor_smoke_flow(db_pool) -> None:
    buyer_service = BuyerService(db_pool)
    fixture = await _prepare_reservable_listing(
        db_pool,
        slug="cmd-shop",
        wb_product_id=5601,
        reward_usdt=Decimal("6.000000"),
        slot_count=1,
        available_slots=1,
    )
    processor = BuyerCommandProcessor(
        buyer_service=buyer_service,
        bot_username="qpi_marketplace_bot",
        display_rub_per_usdt=Decimal("100"),
    )

    start_response = await processor.handle(
        telegram_id=880001,
        username="buyer_cmd",
        text="/start shop_cmd-shop",
    )
    assert "Активные листинги" in start_response.text
    assert str(fixture["listing_id"]) in start_response.text
    assert "wb_product_id=" not in start_response.text
    assert 'товар="' in start_response.text
    assert "~600 ₽" in start_response.text
    assert "USDT" not in start_response.text

    reserve_response = await processor.handle(
        telegram_id=880001,
        username="buyer_cmd",
        text=f"/reserve {fixture['listing_id']}",
    )
    assert "Слот зарезервирован" in reserve_response.text

    assignment_fragment = reserve_response.text.split("assignment_id=")[1]
    assignment_fragment = assignment_fragment.split("\n", maxsplit=1)[0]
    assignment_id = int(assignment_fragment)
    payload = _encode_payload(order_id="ORD-CMD")

    submit_response = await processor.handle(
        telegram_id=880001,
        username="buyer_cmd",
        text=f"/submit_order {assignment_id} {payload}",
    )
    assert "order_verified" in submit_response.text
    assert submit_response.delete_source_message is True

    orders_response = await processor.handle(
        telegram_id=880001,
        username="buyer_cmd",
        text="/my_orders",
    )
    assert "ORD-CMD" in orders_response.text
    assert "order_verified" in orders_response.text
    assert "wb_product_id=" not in orders_response.text
    assert 'товар="' in orders_response.text
    assert "~600 ₽" in orders_response.text
    assert "USDT" not in orders_response.text
