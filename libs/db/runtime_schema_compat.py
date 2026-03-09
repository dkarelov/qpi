from __future__ import annotations

import argparse
import os
from collections.abc import Iterable

import psycopg

from libs.db.psqldef import normalize_database_url

_ACTIVE_ASSIGNMENT_STATUSES = (
    "reserved",
    "order_submitted",
    "order_verified",
    "picked_up_wait_unlock",
    "eligible_for_withdrawal",
    "withdraw_pending_admin",
    "withdraw_sent",
)
_LISTING_JSON_COLUMNS = (
    "wb_tech_sizes_json",
    "wb_characteristics_json",
)
_LISTING_OPTIONAL_COLUMNS = (
    "display_title",
    "wb_source_title",
    "wb_subject_name",
    "wb_brand_name",
    "wb_vendor_code",
    "wb_description",
    "wb_photo_url",
    "reference_price_source",
)


def _resolve_database_url(explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url

    for env_name in ("DATABASE_URL", "TEST_DATABASE_URL"):
        value = os.getenv(env_name)
        if value:
            return value

    raise ValueError("DATABASE_URL (or TEST_DATABASE_URL) must be set")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply additive runtime schema compatibility fixes before psqldef rollout"
    )
    parser.add_argument("command", choices=["apply"])
    parser.add_argument("--database-url", default=None)
    return parser


def _column_exists(cur: psycopg.Cursor, *, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    return cur.fetchone() is not None


def _index_exists(cur: psycopg.Cursor, *, index_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{index_name}",))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _table_exists(cur: psycopg.Cursor, *, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _ensure_user_capability_columns(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="users"):
        return

    cur.execute(
        """
        ALTER TABLE public.users
            ADD COLUMN IF NOT EXISTS is_seller boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS is_buyer boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false
        """
    )
    cur.execute(
        """
        UPDATE public.users
        SET
            is_seller = role = 'seller',
            is_buyer = role = 'buyer',
            is_admin = role = 'admin'
        WHERE NOT is_seller
          AND NOT is_buyer
          AND NOT is_admin
        """
    )
    cur.execute(
        """
        ALTER TABLE public.users
            ALTER COLUMN is_seller SET DEFAULT false,
            ALTER COLUMN is_seller SET NOT NULL,
            ALTER COLUMN is_buyer SET DEFAULT false,
            ALTER COLUMN is_buyer SET NOT NULL,
            ALTER COLUMN is_admin SET DEFAULT false,
            ALTER COLUMN is_admin SET NOT NULL
        """
    )


def _ensure_listing_metadata_columns(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="listings"):
        return

    for column_name in _LISTING_OPTIONAL_COLUMNS:
        if _column_exists(cur, table_name="listings", column_name=column_name):
            continue
        cur.execute(f"ALTER TABLE public.listings ADD COLUMN {column_name} text")

    if not _column_exists(cur, table_name="listings", column_name="reference_price_rub"):
        cur.execute("ALTER TABLE public.listings ADD COLUMN reference_price_rub integer")
    if not _column_exists(cur, table_name="listings", column_name="reference_price_updated_at"):
        cur.execute("ALTER TABLE public.listings ADD COLUMN reference_price_updated_at timestamptz")

    for column_name in _LISTING_JSON_COLUMNS:
        if _column_exists(cur, table_name="listings", column_name=column_name):
            continue
        cur.execute(
            "ALTER TABLE public.listings "
            f"ADD COLUMN {column_name} jsonb NOT NULL DEFAULT '[]'::jsonb"
        )


def _ensure_assignments_wb_product_id(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="assignments"):
        return

    if not _column_exists(cur, table_name="assignments", column_name="wb_product_id"):
        cur.execute("ALTER TABLE public.assignments ADD COLUMN wb_product_id bigint")

    cur.execute(
        """
        UPDATE public.assignments AS a
        SET wb_product_id = l.wb_product_id
        FROM public.listings AS l
        WHERE a.listing_id = l.id
          AND a.wb_product_id IS NULL
        """
    )
    cur.execute("SELECT COUNT(*) FROM public.assignments WHERE wb_product_id IS NULL")
    missing_count = int(cur.fetchone()[0])
    if missing_count:
        raise RuntimeError(
            "runtime schema compatibility failed: assignments.wb_product_id "
            f"still has {missing_count} NULL rows after backfill"
        )

    cur.execute("ALTER TABLE public.assignments ALTER COLUMN wb_product_id SET NOT NULL")

    cur.execute(
        """
        SELECT buyer_user_id, wb_product_id, COUNT(*)
        FROM public.assignments
        WHERE status = ANY(%s)
        GROUP BY buyer_user_id, wb_product_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        (list(_ACTIVE_ASSIGNMENT_STATUSES),),
    )
    duplicate_row = cur.fetchone()
    if duplicate_row is not None:
        buyer_user_id, wb_product_id, duplicate_count = duplicate_row
        raise RuntimeError(
            "runtime schema compatibility failed: duplicate active assignments "
            f"for buyer_user_id={buyer_user_id}, wb_product_id={wb_product_id}, "
            f"count={duplicate_count}"
        )

    if not _index_exists(cur, index_name="idx_assignments_buyer_product_status"):
        cur.execute(
            """
            CREATE INDEX idx_assignments_buyer_product_status
            ON public.assignments USING btree (buyer_user_id, wb_product_id, status)
            """
        )

    if not _index_exists(cur, index_name="uq_assignments_buyer_product_active"):
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


def _ensure_buyer_orders_wb_product_id(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="buyer_orders"):
        return

    if not _column_exists(cur, table_name="buyer_orders", column_name="wb_product_id"):
        cur.execute("ALTER TABLE public.buyer_orders ADD COLUMN wb_product_id bigint")

    cur.execute(
        """
        UPDATE public.buyer_orders AS bo
        SET wb_product_id = COALESCE(a.wb_product_id, l.wb_product_id)
        FROM public.assignments AS a
        JOIN public.listings AS l ON l.id = a.listing_id
        WHERE bo.assignment_id = a.id
          AND bo.wb_product_id IS NULL
        """
    )
    cur.execute("SELECT COUNT(*) FROM public.buyer_orders WHERE wb_product_id IS NULL")
    missing_count = int(cur.fetchone()[0])
    if missing_count:
        raise RuntimeError(
            "runtime schema compatibility failed: buyer_orders.wb_product_id "
            f"still has {missing_count} NULL rows after backfill"
        )

    cur.execute("ALTER TABLE public.buyer_orders ALTER COLUMN wb_product_id SET NOT NULL")


def apply_runtime_schema_compatibility(database_url: str) -> None:
    normalized_database_url = normalize_database_url(database_url)
    with psycopg.connect(normalized_database_url) as conn:
        with conn.cursor() as cur:
            _ensure_user_capability_columns(cur)
            _ensure_listing_metadata_columns(cur)
            _ensure_assignments_wb_product_id(cur)
            _ensure_buyer_orders_wb_product_id(cur)
        conn.commit()


def cli(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        database_url = _resolve_database_url(args.database_url)
        apply_runtime_schema_compatibility(database_url)
        return 0
    except (RuntimeError, ValueError, psycopg.Error) as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
