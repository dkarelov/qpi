from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from libs.config.settings import BotApiSettings
from libs.domain.buyer import BuyerService
from libs.domain.seller import SellerService
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from tests.utils import run_schema_apply


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
