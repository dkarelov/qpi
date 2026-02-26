from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from libs.domain.daily_report import (
    DailyReportScrapperService,
    classify_token_invalidation_source,
)
from libs.integrations.wb_reports import WbReportApiError
from libs.security.token_cipher import encrypt_token
from tests.helpers import create_listing, create_user

_EXPECTED_REPORT_COLUMNS = {
    "realizationreport_id",
    "date_from",
    "date_to",
    "create_dt",
    "currency_name",
    "rrd_id",
    "gi_id",
    "subject_name",
    "nm_id",
    "brand_name",
    "sa_name",
    "ts_name",
    "quantity",
    "retail_price",
    "retail_amount",
    "office_name",
    "supplier_oper_name",
    "order_dt",
    "sale_dt",
    "rr_dt",
    "retail_price_withdisc_rub",
    "delivery_amount",
    "return_amount",
    "supplier_promo",
    "ppvz_spp_prc",
    "ppvz_for_pay",
    "ppvz_office_name",
    "ppvz_office_id",
    "sticker_id",
    "site_country",
    "assembly_id",
    "srid",
    "report_type",
    "order_uid",
    "delivery_method",
    "uuid_promocode",
    "sale_price_promocode_discount_prc",
}


class StubSuccessReportClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def fetch_report_detail_page(self, **kwargs):
        return [dict(row) for row in self._rows]


class StubErrorReportClient:
    def __init__(self, error: WbReportApiError):
        self._error = error

    async def fetch_report_detail_page(self, **kwargs):
        raise self._error


async def _prepare_shop_with_token(
    db_pool,
    *,
    seller_telegram_id: int,
    slug: str,
    token_plaintext: str,
    cipher_key: str,
) -> int:
    async with db_pool.connection() as conn:
        async with conn.transaction():
            seller_user_id = await create_user(
                conn,
                telegram_id=seller_telegram_id,
                role="seller",
                username=f"seller_{seller_telegram_id}",
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO shops (
                        seller_user_id,
                        slug,
                        title,
                        wb_token_ciphertext,
                        wb_token_status,
                        wb_token_status_source
                    )
                    VALUES (%s, %s, %s, %s, 'valid', 'manual')
                    RETURNING id
                    """,
                    (
                        seller_user_id,
                        slug,
                        f"Shop {slug}",
                        encrypt_token(token_plaintext, cipher_key),
                    ),
                )
                shop_row = await cur.fetchone()
                shop_id = shop_row["id"]

            await create_listing(
                conn,
                shop_id=shop_id,
                seller_user_id=seller_user_id,
                wb_product_id=900001,
                reward_usdt=Decimal("1.000000"),
                slot_count=1,
                available_slots=1,
                status="active",
            )

    return shop_id


@pytest.mark.asyncio
async def test_daily_report_scrapper_ingests_projected_columns_and_deduplicates(db_pool) -> None:
    cipher_key = "phase5-test-key"
    await _prepare_shop_with_token(
        db_pool,
        seller_telegram_id=860001,
        slug="phase5-shop",
        token_plaintext="wb-token-1",
        cipher_key=cipher_key,
    )

    sample_rows = [
        {
            "realizationreport_id": 401,
            "date_from": "2026-02-24",
            "date_to": "2026-02-26",
            "create_dt": "2026-02-26T10:11:12Z",
            "currency_name": "RUB",
            "rrd_id": 100001,
            "gi_id": 200001,
            "subject_name": "subject",
            "nm_id": 300001,
            "brand_name": "brand",
            "sa_name": "sa",
            "ts_name": "ts",
            "quantity": 1,
            "retail_price": "999.50",
            "retail_amount": "999.50",
            "office_name": "office",
            "supplier_oper_name": "Продажа",
            "order_dt": "2026-02-25T10:00:00Z",
            "sale_dt": "2026-02-26T10:00:00Z",
            "rr_dt": "2026-02-26T10:10:00Z",
            "retail_price_withdisc_rub": "899.10",
            "delivery_amount": 1,
            "return_amount": 0,
            "supplier_promo": "0",
            "ppvz_spp_prc": "0",
            "ppvz_for_pay": "450.00",
            "ppvz_office_name": "office2",
            "ppvz_office_id": 77,
            "sticker_id": "sticker",
            "site_country": "RU",
            "assembly_id": 15,
            "srid": "order-srid-1",
            "report_type": 1,
            "order_uid": "uid-1",
            "delivery_method": "pickup",
            "uuid_promocode": "promo-1",
            "sale_price_promocode_discount_prc": "3.14",
            "ignored_field": "must_not_be_stored",
        },
        {
            "rrd_id": 100002,
            "srid": None,
        },
    ]

    service = DailyReportScrapperService(
        db_pool,
        token_cipher_key=cipher_key,
        wb_client=StubSuccessReportClient(sample_rows),
        concurrency=2,
        request_limit=100,
        max_retries=0,
        retry_delay_seconds=0.1,
        days_back=3,
    )

    first = await service.run_once()
    second = await service.run_once()

    assert first.shops_total == 1
    assert first.shops_processed == 1
    assert first.rows_seen == 2
    assert first.rows_upserted == 1
    assert first.rows_skipped == 1

    assert second.shops_total == 1
    assert second.shops_processed == 1
    assert second.rows_upserted == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'wb_report_rows'
                ORDER BY ordinal_position ASC
                """
            )
            columns = {row["column_name"] for row in await cur.fetchall()}
            assert columns == _EXPECTED_REPORT_COLUMNS

            await cur.execute("SELECT COUNT(*) AS count FROM wb_report_rows")
            count_row = await cur.fetchone()
            assert count_row["count"] == 1

            await cur.execute(
                """
                SELECT
                    realizationreport_id,
                    date_from,
                    date_to,
                    currency_name,
                    rrd_id,
                    srid,
                    report_type,
                    delivery_method,
                    uuid_promocode,
                    sale_price_promocode_discount_prc
                FROM wb_report_rows
                WHERE rrd_id = 100001
                  AND srid = 'order-srid-1'
                """
            )
            row = await cur.fetchone()
            assert row["realizationreport_id"] == 401
            assert row["date_from"] == date(2026, 2, 24)
            assert row["date_to"] == date(2026, 2, 26)
            assert row["currency_name"] == "RUB"
            assert row["rrd_id"] == 100001
            assert row["srid"] == "order-srid-1"
            assert row["report_type"] == 1
            assert row["delivery_method"] == "pickup"
            assert row["uuid_promocode"] == "promo-1"
            assert row["sale_price_promocode_discount_prc"] == Decimal("3.140000")


@pytest.mark.asyncio
async def test_daily_report_scrapper_invalidates_token_and_pauses_listings_on_401(db_pool) -> None:
    cipher_key = "phase5-test-key"
    shop_id = await _prepare_shop_with_token(
        db_pool,
        seller_telegram_id=860002,
        slug="phase5-shop-invalid",
        token_plaintext="wb-token-invalid",
        cipher_key=cipher_key,
    )

    service = DailyReportScrapperService(
        db_pool,
        token_cipher_key=cipher_key,
        wb_client=StubErrorReportClient(
            WbReportApiError(status_code=401, message="token expired")
        ),
        concurrency=1,
        request_limit=100,
        max_retries=0,
        retry_delay_seconds=0.1,
        days_back=3,
    )

    result = await service.run_once()

    assert result.shops_total == 1
    assert result.shops_failed == 1
    assert result.shops_invalidated == 1

    async with db_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT wb_token_status, wb_token_status_source
                FROM shops
                WHERE id = %s
                """,
                (shop_id,),
            )
            shop = await cur.fetchone()
            assert shop["wb_token_status"] == "expired"
            assert shop["wb_token_status_source"] == "scrapper_401_token_expired"

            await cur.execute(
                """
                SELECT status, pause_source
                FROM listings
                WHERE shop_id = %s
                """,
                (shop_id,),
            )
            listing = await cur.fetchone()
            assert listing["status"] == "paused"
            assert listing["pause_source"] == "scrapper_401_token_expired"


def test_classify_token_invalidation_source() -> None:
    assert classify_token_invalidation_source(
        401, "token expired"
    ) == "scrapper_401_token_expired"
    assert classify_token_invalidation_source(
        401, "user withdrawn by owner"
    ) == "scrapper_401_withdrawn"
    assert classify_token_invalidation_source(401, "unknown auth error") is None
    assert classify_token_invalidation_source(500, "token expired") is None
