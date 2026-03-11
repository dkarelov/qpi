from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from libs.config.settings import BotApiSettings
from libs.domain.buyer import BuyerService
from libs.domain.seller import SellerService
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from tests.helpers import create_account, create_listing, create_shop, create_user
from tests.utils import run_runtime_schema_compat_apply, run_schema_apply


def _build_runtime() -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [1],
            "DISPLAY_RUB_PER_USDT": "100",
        }
    )
    return TelegramWebhookRuntime(settings=settings)


@pytest.mark.asyncio
async def test_runtime_post_init_fails_when_required_schema_columns_are_missing() -> None:
    runtime = _build_runtime()
    rows = [
        {"table_name": "users", "column_name": "id"},
        {"table_name": "users", "column_name": "role"},
        {"table_name": "listings", "column_name": "id"},
        {"table_name": "listings", "column_name": "wb_product_id"},
    ]

    class _FakeCursor:
        async def execute(self, query, params=None) -> None:
            self.query = query
            self.params = params

        async def fetchall(self):
            return rows

    class _FakeConn:
        @asynccontextmanager
        async def cursor(self, row_factory=None):
            yield _FakeCursor()

    @asynccontextmanager
    async def _fake_connection():
        yield _FakeConn()

    runtime._db_pool = SimpleNamespace(
        open=AsyncMock(return_value=None),
        check=AsyncMock(return_value=None),
        close=AsyncMock(return_value=None),
        connection=_fake_connection,
    )

    with pytest.raises(RuntimeError, match="users.is_admin"):
        await runtime._post_init(SimpleNamespace())

    assert runtime._ready is False
    assert runtime._startup_error is not None
    assert "users.is_seller" in runtime._startup_error
    assert "assignments.wb_product_id" in runtime._startup_error
    assert runtime._health_payload()["status"] == "startup_failed"


@pytest.mark.asyncio
async def test_schema_apply_recovers_runtime_compatibility_from_pre_capability_shape(
    isolated_database: str,
    db_pool,
) -> None:
    with psycopg.connect(isolated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE public.users
                    DROP COLUMN IF EXISTS is_seller,
                    DROP COLUMN IF EXISTS is_buyer,
                    DROP COLUMN IF EXISTS is_admin
                """
            )
            cur.execute(
                """
                ALTER TABLE public.listings
                    DROP COLUMN IF EXISTS display_title,
                    DROP COLUMN IF EXISTS wb_source_title,
                    DROP COLUMN IF EXISTS wb_subject_name,
                    DROP COLUMN IF EXISTS wb_brand_name,
                    DROP COLUMN IF EXISTS wb_vendor_code,
                    DROP COLUMN IF EXISTS wb_description,
                    DROP COLUMN IF EXISTS wb_photo_url,
                    DROP COLUMN IF EXISTS wb_tech_sizes_json,
                    DROP COLUMN IF EXISTS wb_characteristics_json,
                    DROP COLUMN IF EXISTS reference_price_rub,
                    DROP COLUMN IF EXISTS reference_price_source,
                    DROP COLUMN IF EXISTS reference_price_updated_at
                """
            )

    run_schema_apply(isolated_database)

    seller_service = SellerService(db_pool)
    buyer_service = BuyerService(db_pool)

    seller = await seller_service.bootstrap_seller(
        telegram_id=991001,
        username="schema_recover_seller",
    )
    buyer = await buyer_service.bootstrap_buyer(
        telegram_id=991002,
        username="schema_recover_buyer",
    )

    assert seller.user_id > 0
    assert buyer.user_id > 0


@pytest.mark.asyncio
async def test_runtime_schema_compat_apply_backfills_legacy_assignment_product_ids(
    isolated_database: str,
) -> None:
    async with await psycopg.AsyncConnection.connect(isolated_database) as conn:
        seller_user_id = await create_user(
            conn,
            telegram_id=992001,
            role="seller",
            username="compat_seller",
        )
        buyer_user_id = await create_user(
            conn,
            telegram_id=992002,
            role="buyer",
            username="compat_buyer",
        )
        shop_id = await create_shop(
            conn,
            seller_user_id=seller_user_id,
            slug="compat-shop",
            title="Compat Shop",
        )
        listing_id = await create_listing(
            conn,
            shop_id=shop_id,
            seller_user_id=seller_user_id,
            wb_product_id=552892532,
            search_phrase="бумага а4",
            reward_usdt=Decimal("0.130000"),
            slot_count=2,
            available_slots=1,
            status="active",
            reference_price_rub=392,
            reference_price_source="manual",
        )

        async with conn.cursor() as cur:
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
                VALUES (%s, %s, %s, 'order_verified', %s, %s, %s)
                RETURNING id
                """,
                (
                    listing_id,
                    buyer_user_id,
                    552892532,
                    Decimal("0.130000"),
                    datetime.now(UTC) + timedelta(hours=2),
                    "compat-assignment",
                ),
            )
            assignment_id = int((await cur.fetchone())[0])
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
                VALUES (%s, %s, %s, %s, %s, %s, 1, '{}'::jsonb, 'plugin_base64')
                """,
                (
                    assignment_id,
                    listing_id,
                    buyer_user_id,
                    "compat-order-1",
                    552892532,
                    datetime.now(UTC),
                ),
            )
        await conn.commit()

    with psycopg.connect(isolated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE public.assignments DROP COLUMN IF EXISTS wb_product_id")
            cur.execute("ALTER TABLE public.buyer_orders DROP COLUMN IF EXISTS wb_product_id")

    run_runtime_schema_compat_apply(isolated_database)
    run_runtime_schema_compat_apply(isolated_database)
    run_schema_apply(isolated_database)

    with psycopg.connect(isolated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wb_product_id FROM public.assignments WHERE id = %s",
                (assignment_id,),
            )
            assert cur.fetchone()[0] == 552892532
            cur.execute(
                "SELECT wb_product_id FROM public.buyer_orders WHERE assignment_id = %s",
                (assignment_id,),
            )
            assert cur.fetchone()[0] == 552892532
            cur.execute("SELECT to_regclass('public.idx_assignments_buyer_product_status')")
            assert cur.fetchone()[0] == "idx_assignments_buyer_product_status"
            cur.execute("SELECT to_regclass('public.uq_assignments_buyer_product_active')")
            assert cur.fetchone()[0] == "uq_assignments_buyer_product_active"


@pytest.mark.asyncio
async def test_runtime_schema_compat_apply_backfills_legacy_withdrawal_and_assignment_statuses(
    isolated_database: str,
) -> None:
    with psycopg.connect(isolated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE public.assignments DROP CONSTRAINT assignments_status_check"
            )
            cur.execute(
                """
                ALTER TABLE public.assignments
                ADD CONSTRAINT assignments_status_check CHECK (
                    status = ANY (
                        ARRAY[
                            'reserved'::text,
                            'order_submitted'::text,
                            'order_verified'::text,
                            'picked_up_wait_unlock'::text,
                            'eligible_for_withdrawal'::text,
                            'withdraw_pending_admin'::text,
                            'withdraw_sent'::text,
                            'expired_2h'::text,
                            'wb_invalid'::text,
                            'returned_within_14d'::text,
                            'delivery_expired'::text
                        ]
                    )
                )
                """
            )
            cur.execute(
                "ALTER TABLE public.withdrawal_requests "
                "DROP CONSTRAINT withdrawal_requests_status_check"
            )
            cur.execute(
                """
                ALTER TABLE public.withdrawal_requests
                ADD CONSTRAINT withdrawal_requests_status_check CHECK (
                    status = ANY (
                        ARRAY[
                            'withdraw_pending_admin'::text,
                            'approved'::text,
                            'rejected'::text,
                            'withdraw_sent'::text
                        ]
                    )
                )
                """
            )
            cur.execute("ALTER TABLE public.accounts DROP CONSTRAINT accounts_account_kind_check")
            cur.execute(
                """
                ALTER TABLE public.accounts
                ADD CONSTRAINT accounts_account_kind_check CHECK (
                    account_kind = ANY (
                        ARRAY[
                            'seller_available'::text,
                            'seller_collateral'::text,
                            'buyer_available'::text,
                            'buyer_withdraw_pending'::text,
                            'reward_reserved'::text,
                            'system_payout'::text
                        ]
                    )
                )
                """
            )
            cur.execute("DROP INDEX IF EXISTS public.uq_assignments_buyer_product_active")
            cur.execute(
                """
                CREATE UNIQUE INDEX uq_assignments_buyer_product_active
                ON public.assignments USING btree (buyer_user_id, wb_product_id)
                WHERE (
                    status = ANY (
                        ARRAY[
                            'reserved'::text,
                            'order_submitted'::text,
                            'order_verified'::text,
                            'picked_up_wait_unlock'::text,
                            'eligible_for_withdrawal'::text,
                            'withdraw_pending_admin'::text,
                            'withdraw_sent'::text
                        ]
                    )
                )
                """
            )

    async with await psycopg.AsyncConnection.connect(isolated_database) as conn:
        seller_user_id = await create_user(
            conn,
            telegram_id=993001,
            role="seller",
            username="compat_seller_status",
        )
        buyer_user_id = await create_user(
            conn,
            telegram_id=993002,
            role="buyer",
            username="compat_buyer_status",
        )
        shop_id = await create_shop(
            conn,
            seller_user_id=seller_user_id,
            slug="compat-status-shop",
            title="Compat Status Shop",
        )
        listing_id = await create_listing(
            conn,
            shop_id=shop_id,
            seller_user_id=seller_user_id,
            wb_product_id=552892540,
            search_phrase="термокружка",
            reward_usdt=Decimal("1.000000"),
            slot_count=1,
            available_slots=0,
            status="active",
            reference_price_rub=990,
            reference_price_source="manual",
        )
        buyer_available_account_id = await create_account(
            conn,
            owner_user_id=buyer_user_id,
            account_code=f"user:{buyer_user_id}:buyer_available",
            account_kind="buyer_available",
            balance=Decimal("0.000000"),
        )
        buyer_pending_account_id = await create_account(
            conn,
            owner_user_id=buyer_user_id,
            account_code=f"user:{buyer_user_id}:buyer_withdraw_pending",
            account_kind="buyer_withdraw_pending",
            balance=Decimal("1.000000"),
        )

        async with conn.cursor() as cur:
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
                VALUES (%s, %s, %s, 'eligible_for_withdrawal', %s, %s, %s)
                RETURNING id
                """,
                (
                    listing_id,
                    buyer_user_id,
                    552892540,
                    Decimal("1.000000"),
                    datetime.now(UTC) + timedelta(hours=2),
                    "compat-assignment-status",
                ),
            )
            assignment_id = int((await cur.fetchone())[0])
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
                VALUES (%s, %s, %s, %s, 'approved', %s, %s)
                RETURNING id
                """,
                (
                    buyer_user_id,
                    buyer_available_account_id,
                    buyer_pending_account_id,
                    Decimal("1.000000"),
                    "UQ_COMPAT",
                    "compat-withdraw-approved",
                ),
            )
            withdrawal_request_id = int((await cur.fetchone())[0])
        await conn.commit()

    run_runtime_schema_compat_apply(isolated_database)
    run_schema_apply(isolated_database)

    with psycopg.connect(isolated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.assignments WHERE id = %s", (assignment_id,))
            assert cur.fetchone()[0] == "withdraw_sent"
            cur.execute(
                """
                SELECT requester_user_id, requester_role, status
                FROM public.withdrawal_requests
                WHERE id = %s
                """,
                (withdrawal_request_id,),
            )
            requester_user_id, requester_role, status = cur.fetchone()
            assert requester_user_id == buyer_user_id
            assert requester_role == "buyer"
            assert status == "withdraw_pending_admin"
            cur.execute("SELECT to_regclass('public.uq_withdrawal_requests_buyer_active')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.uq_withdrawal_requests_requester_active')")
            assert cur.fetchone()[0] == "uq_withdrawal_requests_requester_active"
            cur.execute(
                """
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public'
                  AND t.relname = 'accounts'
                  AND c.conname = 'accounts_account_kind_check'
                """
            )
            assert "seller_withdraw_pending" in cur.fetchone()[0]
