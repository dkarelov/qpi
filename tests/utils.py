from __future__ import annotations

from pathlib import Path

import psycopg

from libs.db.psqldef import run_psqldef

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = PROJECT_ROOT / "schema" / "schema.sql"
EMPTY_SCHEMA_FILE = PROJECT_ROOT / "schema" / "empty.sql"


def reset_public_schema(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")


def run_schema_apply(database_url: str) -> None:
    run_psqldef(database_url, mode="apply", schema_file=SCHEMA_FILE)


def run_schema_drop(database_url: str) -> None:
    run_psqldef(
        database_url,
        mode="apply",
        schema_file=EMPTY_SCHEMA_FILE,
        enable_drop=True,
    )


def table_exists(database_url: str, table_name: str) -> bool:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            row = cur.fetchone()
            return row is not None and row[0] is not None
