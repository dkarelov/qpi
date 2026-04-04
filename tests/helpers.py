from __future__ import annotations

from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Json


async def create_user(
    conn: AsyncConnection,
    *,
    telegram_id: int,
    role: str,
    username: str,
) -> int:
    is_seller = role == "seller"
    is_buyer = role == "buyer"
    is_admin = role == "admin"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO users (
                telegram_id,
                role,
                username,
                is_seller,
                is_buyer,
                is_admin
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (telegram_id, role, username, is_seller, is_buyer, is_admin),
        )
        row = await cur.fetchone()
        return row["id"]


async def create_account(
    conn: AsyncConnection,
    *,
    owner_user_id: int | None,
    account_code: str,
    account_kind: str,
    balance: Decimal,
) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO accounts (
                owner_user_id,
                account_code,
                account_kind,
                current_balance_usdt
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (owner_user_id, account_code, account_kind, balance),
        )
        row = await cur.fetchone()
        return row["id"]


async def create_shop(
    conn: AsyncConnection,
    *,
    seller_user_id: int,
    slug: str,
    title: str,
) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO shops (
                seller_user_id,
                slug,
                title,
                wb_token_ciphertext,
                wb_token_status
            )
            VALUES (%s, %s, %s, %s, 'valid')
            RETURNING id
            """,
            (seller_user_id, slug, title, "encrypted-token"),
        )
        row = await cur.fetchone()
        return row["id"]


async def create_listing(
    conn: AsyncConnection,
    *,
    shop_id: int,
    seller_user_id: int,
    wb_product_id: int,
    search_phrase: str = "тестовый запрос",
    reward_usdt: Decimal,
    slot_count: int,
    available_slots: int,
    status: str = "active",
    display_title: str | None = None,
    wb_source_title: str | None = None,
    wb_subject_name: str | None = None,
    wb_brand_name: str | None = None,
    wb_vendor_code: str | None = None,
    wb_description: str | None = None,
    wb_photo_url: str | None = None,
    wb_tech_sizes: list[str] | None = None,
    wb_characteristics: list[dict[str, str]] | None = None,
    review_phrases: list[str] | None = None,
    reference_price_rub: int | None = None,
    reference_price_source: str | None = None,
) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO listings (
                shop_id,
                seller_user_id,
                wb_product_id,
                display_title,
                wb_source_title,
                wb_subject_name,
                wb_brand_name,
                wb_vendor_code,
                wb_description,
                wb_photo_url,
                wb_tech_sizes_json,
                wb_characteristics_json,
                review_phrases_json,
                reference_price_rub,
                reference_price_source,
                search_phrase,
                reward_usdt,
                slot_count,
                available_slots,
                collateral_required_usdt,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                shop_id,
                seller_user_id,
                wb_product_id,
                display_title or search_phrase,
                wb_source_title,
                wb_subject_name,
                wb_brand_name,
                wb_vendor_code,
                wb_description,
                wb_photo_url,
                Json(wb_tech_sizes or []),
                Json(wb_characteristics or []),
                Json(review_phrases or []),
                reference_price_rub,
                reference_price_source,
                search_phrase,
                reward_usdt,
                slot_count,
                available_slots,
                reward_usdt * slot_count * Decimal("1.01"),
                status,
            ),
        )
        row = await cur.fetchone()
        return row["id"]
