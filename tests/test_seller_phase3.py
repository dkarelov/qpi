from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.errors import InvalidStateError
from libs.domain.ledger import FinanceService
from libs.domain.seller import SellerService
from libs.integrations.wb import WbPingResult
from libs.security.token_cipher import encrypt_token
from services.bot_api.seller_handlers import SellerCommandProcessor
from tests.helpers import create_user


class StubWbPingClient:
    def __init__(self, *, valid: bool, message: str = "ok") -> None:
        self._valid = valid
        self._message = message

    async def validate_token(self, token: str) -> WbPingResult:
        if self._valid:
            return WbPingResult(valid=True, status_code=200, message="ok")
        return WbPingResult(valid=False, status_code=401, message=self._message)


async def _set_account_balance(db_pool, *, account_id: int, balance: Decimal) -> None:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE accounts
                    SET current_balance_usdt = %s,
                        updated_at = timezone('utc', now())
                    WHERE id = %s
                    """,
                    (balance, account_id),
                )


async def _ensure_reward_reserved_account(db_pool) -> int:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO accounts (
                        owner_user_id,
                        account_code,
                        account_kind
                    )
                    VALUES (NULL, 'system:reward_reserved', 'reward_reserved')
                    ON CONFLICT (account_code)
                    DO UPDATE SET updated_at = timezone('utc', now())
                    RETURNING id
                    """
                )
                row = await cur.fetchone()
                return row["id"]


@pytest.mark.asyncio
async def test_seller_bootstrap_is_idempotent_and_creates_accounts(db_pool) -> None:
    service = SellerService(db_pool)

    first = await service.bootstrap_seller(telegram_id=7001, username="seller_a")
    second = await service.bootstrap_seller(telegram_id=7001, username="seller_a")

    assert first.created_user is True
    assert second.created_user is False
    assert first.user_id == second.user_id
    assert first.seller_available_account_id == second.seller_available_account_id
    assert first.seller_collateral_account_id == second.seller_collateral_account_id

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM accounts
                WHERE owner_user_id = %s
                  AND account_kind IN ('seller_available', 'seller_collateral')
                """,
                (first.user_id,),
            )
            row = await cur.fetchone()
            assert row["count"] == 2


@pytest.mark.asyncio
async def test_admin_can_bootstrap_seller_and_operate_seller_flow(db_pool) -> None:
    service = SellerService(db_pool)

    async with db_pool.connection() as conn:
        async with conn.transaction():
            admin_user_id = await create_user(
                conn,
                telegram_id=7099,
                role="admin",
                username="admin_seller",
            )

    result = await service.bootstrap_seller(telegram_id=7099, username="admin_seller")

    assert result.created_user is False
    assert result.user_id == admin_user_id

    shop = await service.create_shop(seller_user_id=result.user_id, title="Admin Seller Shop")
    shops = await service.list_shops(seller_user_id=result.user_id)

    assert [item.shop_id for item in shops] == [shop.shop_id]

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT role FROM users WHERE id = %s", (admin_user_id,))
            row = await cur.fetchone()
            assert row["role"] == "admin"


@pytest.mark.asyncio
async def test_shop_slug_uniqueness_and_multi_listing_crud(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7002, username="seller_b")

    shop_one = await service.create_shop(seller_user_id=seller.user_id, title="My Test Shop")
    shop_two = await service.create_shop(seller_user_id=seller.user_id, title="Second Test Shop")
    assert shop_one.slug != shop_two.slug

    await service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop_one.shop_id,
        token_ciphertext=encrypt_token("token-1", "test-key"),
    )
    await service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop_two.shop_id,
        token_ciphertext=encrypt_token("token-2", "test-key"),
    )

    listing_one = await service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop_one.shop_id,
        wb_product_id=10001,
        discount_percent=10,
        reward_usdt=Decimal("3.000000"),
        slot_count=2,
    )
    listing_two = await service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop_two.shop_id,
        wb_product_id=10002,
        discount_percent=15,
        reward_usdt=Decimal("4.000000"),
        slot_count=3,
    )

    listings = await service.list_listings(seller_user_id=seller.user_id)
    assert {row.listing_id for row in listings} == {listing_one.listing_id, listing_two.listing_id}

    delete_shop_one = await service.delete_shop(
        seller_user_id=seller.user_id,
        shop_id=shop_one.shop_id,
        deleted_by_user_id=seller.user_id,
        idempotency_key=f"shop-delete-{shop_one.shop_id}",
    )
    assert delete_shop_one.changed is True

    remaining_shops = await service.list_shops(seller_user_id=seller.user_id)
    assert [row.shop_id for row in remaining_shops] == [shop_two.shop_id]
    remaining_listings = await service.list_listings(seller_user_id=seller.user_id)
    assert [row.listing_id for row in remaining_listings] == [listing_two.listing_id]


@pytest.mark.asyncio
async def test_shop_title_must_be_unique_for_seller(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7990, username="seller_unique")

    await service.create_shop(seller_user_id=seller.user_id, title="Unique Shop")
    with pytest.raises(InvalidStateError, match="shop title already exists"):
        await service.create_shop(seller_user_id=seller.user_id, title="unique shop")


@pytest.mark.asyncio
async def test_shop_slug_transliterates_cyrillic_title(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7991, username="seller_slug")

    shop = await service.create_shop(
        seller_user_id=seller.user_id,
        title="тушенка для всех",
    )

    assert shop.slug == "tushenka_dlya_vseh"


@pytest.mark.asyncio
async def test_shop_rename_regenerates_slug_and_enforces_unique_title(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7992, username="seller_rename")
    first = await service.create_shop(seller_user_id=seller.user_id, title="Alpha Shop")
    await service.create_shop(seller_user_id=seller.user_id, title="Beta Shop")

    renamed = await service.rename_shop(
        seller_user_id=seller.user_id,
        shop_id=first.shop_id,
        title="Gamma Shop",
    )
    assert renamed.title == "Gamma Shop"
    assert renamed.slug == "gamma_shop"

    with pytest.raises(InvalidStateError, match="shop title already exists"):
        await service.rename_shop(
            seller_user_id=seller.user_id,
            shop_id=first.shop_id,
            title="beta shop",
        )


@pytest.mark.asyncio
async def test_token_ping_failure_does_not_persist_token_and_success_does(db_pool) -> None:
    seller_service = SellerService(db_pool)
    seller = await seller_service.bootstrap_seller(telegram_id=7003, username="seller_c")
    shop = await seller_service.create_shop(seller_user_id=seller.user_id, title="Ping Shop")

    fail_processor = SellerCommandProcessor(
        seller_service=seller_service,
        wb_ping_client=StubWbPingClient(valid=False, message="token expired"),
        token_cipher_key="test-key",
        bot_username="qpi_bot",
    )
    fail_response = await fail_processor.handle(
        telegram_id=7003,
        username="seller_c",
        text=f"/token_set {shop.shop_id} bad-token",
    )
    assert "не сохранен" in fail_response.text

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT wb_token_ciphertext, wb_token_status FROM shops WHERE id = %s",
                (shop.shop_id,),
            )
            row = await cur.fetchone()
            assert row["wb_token_ciphertext"] is None
            assert row["wb_token_status"] == "unknown"

    ok_processor = SellerCommandProcessor(
        seller_service=seller_service,
        wb_ping_client=StubWbPingClient(valid=True),
        token_cipher_key="test-key",
        bot_username="qpi_bot",
    )
    ok_response = await ok_processor.handle(
        telegram_id=7003,
        username="seller_c",
        text=f"/token_set {shop.shop_id} good-token",
    )
    assert "сохранен" in ok_response.text

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT wb_token_ciphertext, wb_token_status FROM shops WHERE id = %s",
                (shop.shop_id,),
            )
            row = await cur.fetchone()
            assert row["wb_token_ciphertext"] is not None
            assert row["wb_token_ciphertext"] != "good-token"
            assert row["wb_token_status"] == "valid"


@pytest.mark.asyncio
async def test_listing_activation_requires_token_and_is_idempotent(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7004, username="seller_d")
    shop = await service.create_shop(seller_user_id=seller.user_id, title="Activation Shop")
    listing = await service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        wb_product_id=20001,
        discount_percent=20,
        reward_usdt=Decimal("5.000000"),
        slot_count=2,
    )

    await _set_account_balance(
        db_pool,
        account_id=seller.seller_available_account_id,
        balance=Decimal("10.000000"),
    )

    with pytest.raises(InvalidStateError):
        await service.activate_listing(
            seller_user_id=seller.user_id,
            listing_id=listing.listing_id,
            idempotency_key="activate-1",
        )

    await service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        token_ciphertext=encrypt_token("valid-token", "test-key"),
    )

    first = await service.activate_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        idempotency_key="activate-1",
    )
    second = await service.activate_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        idempotency_key="activate-1",
    )

    assert first.changed is True
    assert second.changed is False

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (seller.seller_collateral_account_id,),
            )
            collateral_row = await cur.fetchone()
            assert collateral_row["current_balance_usdt"] == Decimal("10.000000")

            await cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM balance_holds
                WHERE listing_id = %s
                  AND hold_type = 'collateral'
                  AND status = 'active'
                """,
                (listing.listing_id,),
            )
            holds_row = await cur.fetchone()
            assert holds_row["count"] == 1


@pytest.mark.asyncio
async def test_listing_delete_warning_and_transfer_split(db_pool) -> None:
    seller_service = SellerService(db_pool)
    finance_service = FinanceService(db_pool)

    seller = await seller_service.bootstrap_seller(telegram_id=7005, username="seller_e")
    shop = await seller_service.create_shop(seller_user_id=seller.user_id, title="Delete Shop")
    await seller_service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        token_ciphertext=encrypt_token("valid-token", "test-key"),
    )
    listing = await seller_service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        wb_product_id=30001,
        discount_percent=30,
        reward_usdt=Decimal("10.000000"),
        slot_count=2,
    )

    await _set_account_balance(
        db_pool,
        account_id=seller.seller_available_account_id,
        balance=Decimal("20.000000"),
    )
    await seller_service.activate_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        idempotency_key="activate-delete-case",
    )

    async with db_pool.connection() as conn:
        async with conn.transaction():
            buyer_user_id = await create_user(
                conn,
                telegram_id=7101,
                role="buyer",
                username="buyer_delete",
            )

    reward_reserved_account_id = await _ensure_reward_reserved_account(db_pool)
    await finance_service.create_assignment_reservation(
        listing_id=listing.listing_id,
        buyer_user_id=buyer_user_id,
        seller_collateral_account_id=seller.seller_collateral_account_id,
        reward_reserved_account_id=reward_reserved_account_id,
        idempotency_key="reserve-delete-case",
    )

    preview = await seller_service.get_listing_delete_preview(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
    )
    assert preview.open_assignments_count == 1
    assert preview.assignment_linked_reserved_usdt == Decimal("10.000000")
    assert preview.unassigned_collateral_usdt == Decimal("10.000000")

    processor = SellerCommandProcessor(
        seller_service=seller_service,
        wb_ping_client=StubWbPingClient(valid=True),
        token_cipher_key="test-key",
        bot_username="qpi_bot",
    )
    warning_response = await processor.handle(
        telegram_id=7005,
        username="seller_e",
        text=f"/listing_delete {listing.listing_id}",
    )
    assert "ВНИМАНИЕ" in warning_response.text
    assert "Подтвердите" in warning_response.text

    deleted = await seller_service.delete_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        deleted_by_user_id=seller.user_id,
        idempotency_key="delete-listing-case",
    )
    assert deleted.changed is True
    assert deleted.assignment_transfers_count == 1
    assert deleted.assignment_transferred_usdt == Decimal("10.000000")
    assert deleted.unassigned_collateral_returned_usdt == Decimal("10.000000")

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE account_code = %s
                """,
                (f"user:{buyer_user_id}:buyer_available",),
            )
            buyer_balance = await cur.fetchone()
            assert buyer_balance["current_balance_usdt"] == Decimal("10.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (seller.seller_available_account_id,),
            )
            seller_available = await cur.fetchone()
            assert seller_available["current_balance_usdt"] == Decimal("10.000000")

            await cur.execute(
                "SELECT current_balance_usdt FROM accounts WHERE id = %s",
                (seller.seller_collateral_account_id,),
            )
            seller_collateral = await cur.fetchone()
            assert seller_collateral["current_balance_usdt"] == Decimal("0.000000")

            await cur.execute(
                """
                SELECT current_balance_usdt
                FROM accounts
                WHERE id = %s
                """,
                (reward_reserved_account_id,),
            )
            reward_reserved = await cur.fetchone()
            assert reward_reserved["current_balance_usdt"] == Decimal("0.000000")

            await cur.execute(
                """
                SELECT status, deleted_at
                FROM listings
                WHERE id = %s
                """,
                (listing.listing_id,),
            )
            listing_row = await cur.fetchone()
            assert listing_row["status"] == "paused"
            assert listing_row["deleted_at"] is not None


@pytest.mark.asyncio
async def test_token_invalidation_pauses_active_listings(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7006, username="seller_f")
    shop = await service.create_shop(seller_user_id=seller.user_id, title="Invalidation Shop")
    await service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        token_ciphertext=encrypt_token("valid-token", "test-key"),
    )
    listing = await service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        wb_product_id=40001,
        discount_percent=15,
        reward_usdt=Decimal("2.000000"),
        slot_count=1,
    )
    await _set_account_balance(
        db_pool,
        account_id=seller.seller_available_account_id,
        balance=Decimal("2.000000"),
    )
    await service.activate_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        idempotency_key="activate-invalid-case",
    )

    result = await service.invalidate_shop_token_and_pause(
        shop_id=shop.shop_id,
        source="scrapper_401_token_expired",
        error_message="token expired",
    )
    assert result.changed is True
    assert result.paused_listings_count == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT wb_token_status, wb_token_status_source
                FROM shops
                WHERE id = %s
                """,
                (shop.shop_id,),
            )
            shop_row = await cur.fetchone()
            assert shop_row["wb_token_status"] == "expired"
            assert shop_row["wb_token_status_source"] == "scrapper_401_token_expired"

            await cur.execute(
                """
                SELECT status, pause_source
                FROM listings
                WHERE id = %s
                """,
                (listing.listing_id,),
            )
            listing_row = await cur.fetchone()
            assert listing_row["status"] == "paused"
            assert listing_row["pause_source"] == "scrapper_401_token_expired"


@pytest.mark.asyncio
async def test_shop_delete_warning_via_command_processor(db_pool) -> None:
    seller_service = SellerService(db_pool)
    seller = await seller_service.bootstrap_seller(telegram_id=7007, username="seller_g")
    shop = await seller_service.create_shop(seller_user_id=seller.user_id, title="Warning Shop")

    processor = SellerCommandProcessor(
        seller_service=seller_service,
        wb_ping_client=StubWbPingClient(valid=True),
        token_cipher_key="test-key",
        bot_username="qpi_bot",
    )
    warning = await processor.handle(
        telegram_id=7007,
        username="seller_g",
        text=f"/shop_delete {shop.shop_id}",
    )
    assert "ВНИМАНИЕ" in warning.text
    assert "Подтвердите" in warning.text

    confirmed = await processor.handle(
        telegram_id=7007,
        username="seller_g",
        text=f"/shop_delete {shop.shop_id} confirm",
    )
    assert "Магазин удален" in confirmed.text


@pytest.mark.asyncio
async def test_shop_create_response_hides_internal_slug_and_id(db_pool) -> None:
    seller_service = SellerService(db_pool)
    processor = SellerCommandProcessor(
        seller_service=seller_service,
        wb_ping_client=StubWbPingClient(valid=True),
        token_cipher_key="test-key",
        bot_username="qpi_bot",
    )

    response = await processor.handle(
        telegram_id=7097,
        username="seller_create_msg",
        text="/shop_create тушенка для всех",
    )

    assert "Магазин «тушенка для всех» создан." in response.text
    assert "Ссылка для покупателей" in response.text
    assert "id=" not in response.text
    assert "slug=" not in response.text


@pytest.mark.asyncio
async def test_seller_balance_and_collateral_views(db_pool) -> None:
    service = SellerService(db_pool)
    seller = await service.bootstrap_seller(telegram_id=7008, username="seller_h")
    shop = await service.create_shop(seller_user_id=seller.user_id, title="Balance Shop")
    await service.save_validated_shop_token(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        token_ciphertext=encrypt_token("valid-token", "test-key"),
    )
    listing = await service.create_listing_draft(
        seller_user_id=seller.user_id,
        shop_id=shop.shop_id,
        wb_product_id=50001,
        discount_percent=20,
        reward_usdt=Decimal("3.000000"),
        slot_count=2,
    )

    await _set_account_balance(
        db_pool,
        account_id=seller.seller_available_account_id,
        balance=Decimal("6.000000"),
    )
    await service.activate_listing(
        seller_user_id=seller.user_id,
        listing_id=listing.listing_id,
        idempotency_key="activate-balance-case",
    )

    snapshot = await service.get_seller_balance_snapshot(seller_user_id=seller.user_id)
    assert snapshot.seller_available_usdt == Decimal("0.000000")
    assert snapshot.seller_collateral_usdt == Decimal("6.000000")

    views = await service.list_listing_collateral_views(seller_user_id=seller.user_id)
    assert len(views) == 1
    assert views[0].listing_id == listing.listing_id
    assert views[0].collateral_required_usdt == Decimal("6.000000")
    assert views[0].collateral_locked_usdt == Decimal("6.000000")
