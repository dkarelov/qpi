from __future__ import annotations

import argparse
import os
from collections.abc import Iterable

import psycopg

from libs.db.psqldef import normalize_database_url

_ACTIVE_ASSIGNMENT_STATUSES = (
    "reserved",
    "order_verified",
    "picked_up_wait_unlock",
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
_ACCOUNT_KINDS = (
    "seller_available",
    "seller_collateral",
    "seller_withdraw_pending",
    "buyer_available",
    "buyer_withdraw_pending",
    "reward_reserved",
    "system_payout",
)
_TOKEN_INVALIDATION_SOURCES = (
    "manual",
    "scrapper_401_withdrawn",
    "scrapper_401_token_expired",
    "scrapper_401_unauthorized",
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


def _index_definition(cur: psycopg.Cursor, *, index_name: str) -> str | None:
    cur.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = %s
        """,
        (index_name,),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def _table_exists(cur: psycopg.Cursor, *, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _constraint_definition(
    cur: psycopg.Cursor,
    *,
    table_name: str,
    constraint_name: str,
) -> str | None:
    cur.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND t.relname = %s
          AND c.conname = %s
        """,
        (table_name, constraint_name),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


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


def _ensure_accounts_account_kinds(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="accounts"):
        return

    constraint_def = _constraint_definition(
        cur,
        table_name="accounts",
        constraint_name="accounts_account_kind_check",
    )
    if constraint_def is not None and "seller_withdraw_pending" in constraint_def:
        return

    if constraint_def is not None:
        cur.execute("ALTER TABLE public.accounts DROP CONSTRAINT accounts_account_kind_check")
    cur.execute(
        "ALTER TABLE public.accounts "
        "ADD CONSTRAINT accounts_account_kind_check CHECK ("
        "account_kind = ANY (ARRAY["
        + ", ".join(f"'{kind}'::text" for kind in _ACCOUNT_KINDS)
        + "]))"
    )


def _ensure_system_balance_provisions(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="accounts"):
        return
    if _table_exists(cur, table_name="system_balance_provisions"):
        return

    cur.execute(
        """
        CREATE TABLE public.system_balance_provisions (
            id bigserial PRIMARY KEY,
            account_id bigint NOT NULL REFERENCES public.accounts (id),
            amount_usdt numeric(20,6) NOT NULL CHECK (amount_usdt > 0),
            event_type text NOT NULL,
            metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX idx_system_balance_provisions_account_id
        ON public.system_balance_provisions USING btree (account_id, created_at DESC)
        """
    )


def _ensure_chain_scan_cursor_resume_column(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="chain_scan_cursors"):
        return
    if _column_exists(cur, table_name="chain_scan_cursors", column_name="resume_before_lt"):
        return
    cur.execute("ALTER TABLE public.chain_scan_cursors ADD COLUMN resume_before_lt bigint")


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


def _ensure_token_invalidation_sources(cur: psycopg.Cursor) -> None:
    if _table_exists(cur, table_name="listings"):
        constraint_def = _constraint_definition(
            cur,
            table_name="listings",
            constraint_name="listings_pause_source_check",
        )
        if constraint_def is None or "scrapper_401_unauthorized" not in constraint_def:
            if constraint_def is not None:
                cur.execute(
                    "ALTER TABLE public.listings DROP CONSTRAINT listings_pause_source_check"
                )
            cur.execute(
                "ALTER TABLE public.listings "
                "ADD CONSTRAINT listings_pause_source_check CHECK ("
                "pause_source = ANY (ARRAY["
                + ", ".join(f"'{source}'::text" for source in _TOKEN_INVALIDATION_SOURCES)
                + "]))"
            )

    if _table_exists(cur, table_name="shops"):
        constraint_def = _constraint_definition(
            cur,
            table_name="shops",
            constraint_name="shops_wb_token_status_source_check",
        )
        if constraint_def is None or "scrapper_401_unauthorized" not in constraint_def:
            if constraint_def is not None:
                cur.execute(
                    "ALTER TABLE public.shops DROP CONSTRAINT shops_wb_token_status_source_check"
                )
            cur.execute(
                "ALTER TABLE public.shops "
                "ADD CONSTRAINT shops_wb_token_status_source_check CHECK ("
                "wb_token_status_source = ANY (ARRAY["
                + ", ".join(f"'{source}'::text" for source in _TOKEN_INVALIDATION_SOURCES)
                + "]))"
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

    active_index_def = _index_definition(cur, index_name="uq_assignments_buyer_product_active")
    if active_index_def is not None:
        normalized_def = active_index_def.lower()
        if (
            "order_submitted" in normalized_def
            or "eligible_for_withdrawal" in normalized_def
            or "withdraw_pending_admin" in normalized_def
        ):
            cur.execute("DROP INDEX public.uq_assignments_buyer_product_active")
            active_index_def = None

    if active_index_def is None:
        cur.execute(
            """
            CREATE UNIQUE INDEX uq_assignments_buyer_product_active
            ON public.assignments USING btree (buyer_user_id, wb_product_id)
            WHERE (
                status = ANY (
                    ARRAY[
                        'reserved'::text,
                        'order_verified'::text,
                        'picked_up_wait_unlock'::text,
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


def _ensure_withdrawal_request_requester_columns(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="withdrawal_requests"):
        return

    if not _column_exists(cur, table_name="withdrawal_requests", column_name="requester_user_id"):
        cur.execute("ALTER TABLE public.withdrawal_requests ADD COLUMN requester_user_id bigint")
    if not _column_exists(cur, table_name="withdrawal_requests", column_name="requester_role"):
        cur.execute("ALTER TABLE public.withdrawal_requests ADD COLUMN requester_role text")

    if _column_exists(cur, table_name="withdrawal_requests", column_name="buyer_user_id"):
        cur.execute(
            """
            UPDATE public.withdrawal_requests
            SET requester_user_id = COALESCE(requester_user_id, buyer_user_id),
                requester_role = COALESCE(NULLIF(requester_role, ''), 'buyer')
            WHERE buyer_user_id IS NOT NULL
            """
        )
        cur.execute(
            """
            ALTER TABLE public.withdrawal_requests
                ALTER COLUMN buyer_user_id DROP NOT NULL
            """
        )

    cur.execute(
        """
        UPDATE public.withdrawal_requests
        SET requester_role = 'buyer'
        WHERE requester_role IS NULL
        """
    )

    cur.execute(
        """
        SELECT COUNT(*)
        FROM public.withdrawal_requests
        WHERE requester_user_id IS NULL
           OR requester_role IS NULL
        """
    )
    missing_count = int(cur.fetchone()[0])
    if missing_count:
        raise RuntimeError(
            "runtime schema compatibility failed: withdrawal_requests requester columns "
            f"still have {missing_count} NULL rows after backfill"
        )

    cur.execute(
        """
        SELECT requester_role, requester_user_id, COUNT(*)
        FROM public.withdrawal_requests
        WHERE status = 'withdraw_pending_admin'
        GROUP BY requester_role, requester_user_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    )
    duplicate_pending_row = cur.fetchone()
    if duplicate_pending_row is not None:
        requester_role, requester_user_id, duplicate_count = duplicate_pending_row
        raise RuntimeError(
            "runtime schema compatibility failed: duplicate pending withdrawal requests "
            f"for requester_role={requester_role}, requester_user_id={requester_user_id}, "
            f"count={duplicate_count}"
        )

    cur.execute(
        """
        ALTER TABLE public.withdrawal_requests
            ALTER COLUMN requester_user_id SET NOT NULL,
            ALTER COLUMN requester_role SET NOT NULL
        """
    )

    requester_role_check_def = _constraint_definition(
        cur,
        table_name="withdrawal_requests",
        constraint_name="withdrawal_requests_requester_role_check",
    )
    if requester_role_check_def is None or "seller" not in requester_role_check_def.lower():
        if requester_role_check_def is not None:
            cur.execute(
                "ALTER TABLE public.withdrawal_requests "
                "DROP CONSTRAINT withdrawal_requests_requester_role_check"
            )
        cur.execute(
            """
            ALTER TABLE public.withdrawal_requests
            ADD CONSTRAINT withdrawal_requests_requester_role_check CHECK (
                requester_role = ANY (ARRAY['buyer'::text, 'seller'::text])
            )
            """
        )

    if _index_exists(cur, index_name="uq_withdrawal_requests_buyer_active"):
        cur.execute("DROP INDEX public.uq_withdrawal_requests_buyer_active")

    active_index_def = _index_definition(cur, index_name="uq_withdrawal_requests_requester_active")
    if active_index_def is not None:
        normalized_def = active_index_def.lower()
        if "requester_role" not in normalized_def or "requester_user_id" not in normalized_def:
            cur.execute("DROP INDEX public.uq_withdrawal_requests_requester_active")
            active_index_def = None

    if active_index_def is None:
        cur.execute(
            """
            CREATE UNIQUE INDEX uq_withdrawal_requests_requester_active
            ON public.withdrawal_requests USING btree (requester_role, requester_user_id)
            WHERE (status = 'withdraw_pending_admin'::text)
            """
        )


def _normalize_withdrawal_and_assignment_statuses(cur: psycopg.Cursor) -> None:
    if _table_exists(cur, table_name="withdrawal_requests"):
        cur.execute(
            """
            UPDATE public.withdrawal_requests
            SET status = 'withdraw_pending_admin'
            WHERE status = 'approved'
            """
        )

    if _table_exists(cur, table_name="assignments"):
        cur.execute(
            """
            UPDATE public.assignments
            SET status = 'order_verified',
                updated_at = timezone('utc', now())
            WHERE status = 'order_submitted'
            """
        )
        cur.execute(
            """
            UPDATE public.assignments
            SET status = 'withdraw_sent',
                updated_at = timezone('utc', now())
            WHERE status = ANY (
                ARRAY[
                    'eligible_for_withdrawal'::text,
                    'withdraw_pending_admin'::text
                ]
            )
            """
        )


def _ensure_wb_report_rows_wb_srid(cur: psycopg.Cursor) -> None:
    if not _table_exists(cur, table_name="wb_report_rows"):
        return

    has_legacy_srid = _column_exists(cur, table_name="wb_report_rows", column_name="srid")
    if not _column_exists(cur, table_name="wb_report_rows", column_name="wb_srid"):
        cur.execute("ALTER TABLE public.wb_report_rows ADD COLUMN wb_srid text")

    if has_legacy_srid:
        cur.execute(
            """
            UPDATE public.wb_report_rows
            SET wb_srid = srid
            WHERE wb_srid IS NULL
            """
        )

    cur.execute("SELECT COUNT(*) FROM public.wb_report_rows WHERE wb_srid IS NULL")
    missing_count = int(cur.fetchone()[0])
    if missing_count:
        raise RuntimeError(
            "runtime schema compatibility failed: wb_report_rows.wb_srid "
            f"still has {missing_count} NULL rows after backfill"
        )

    cur.execute("ALTER TABLE public.wb_report_rows ALTER COLUMN wb_srid SET NOT NULL")

    primary_key_def = _constraint_definition(
        cur,
        table_name="wb_report_rows",
        constraint_name="wb_report_rows_pkey",
    )
    if primary_key_def and "srid" in primary_key_def and "wb_srid" not in primary_key_def:
        cur.execute("ALTER TABLE public.wb_report_rows DROP CONSTRAINT wb_report_rows_pkey")
        cur.execute(
            """
            ALTER TABLE public.wb_report_rows
            ADD CONSTRAINT wb_report_rows_pkey PRIMARY KEY (rrd_id, wb_srid)
            """
        )

    if _index_exists(cur, index_name="idx_wb_report_rows_srid"):
        cur.execute("DROP INDEX public.idx_wb_report_rows_srid")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wb_report_rows_srid
        ON public.wb_report_rows USING btree (wb_srid)
        """
    )


def apply_runtime_schema_compatibility(database_url: str) -> None:
    normalized_database_url = normalize_database_url(database_url)
    with psycopg.connect(normalized_database_url) as conn:
        with conn.cursor() as cur:
            _ensure_accounts_account_kinds(cur)
            _ensure_system_balance_provisions(cur)
            _ensure_chain_scan_cursor_resume_column(cur)
            _ensure_user_capability_columns(cur)
            _ensure_listing_metadata_columns(cur)
            _ensure_token_invalidation_sources(cur)
            _ensure_assignments_wb_product_id(cur)
            _ensure_buyer_orders_wb_product_id(cur)
            _ensure_withdrawal_request_requester_columns(cur)
            _normalize_withdrawal_and_assignment_statuses(cur)
            _ensure_wb_report_rows_wb_srid(cur)
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
