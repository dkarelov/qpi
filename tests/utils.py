from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql

from libs.db.psqldef import run_psqldef

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = PROJECT_ROOT / "schema" / "schema.sql"
EMPTY_SCHEMA_FILE = PROJECT_ROOT / "schema" / "empty.sql"


@dataclass(frozen=True)
class ParsedDatabaseUrl:
    scheme: str
    host: str | None
    dbname: str


def parse_database_url(database_url: str) -> ParsedDatabaseUrl:
    normalized = database_url.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql://" + normalized[len("postgres://") :]
    if normalized.startswith("postgresql+psycopg://"):
        normalized = "postgresql://" + normalized[len("postgresql+psycopg://") :]

    parsed = urlparse(normalized)
    if parsed.scheme != "postgresql":
        raise ValueError("TEST_DATABASE_URL must use postgresql:// scheme")

    dbname = parsed.path.lstrip("/")
    if not dbname:
        raise ValueError("TEST_DATABASE_URL must include a database name")

    return ParsedDatabaseUrl(scheme=parsed.scheme, host=parsed.hostname, dbname=dbname)


def assert_safe_test_database(
    database_url: str,
    *,
    require_scratch_name: bool = False,
) -> None:
    parsed = parse_database_url(database_url)
    lowered = parsed.dbname.lower()
    if "test" not in lowered:
        raise RuntimeError(
            "Refusing to run integration tests against a non-test database "
            f"('{parsed.dbname}'). Use dedicated *_test database."
        )
    if require_scratch_name and not any(
        token in lowered for token in ("scratch", "tmp", "disposable")
    ):
        raise RuntimeError(
            "Refusing to run destructive migration smoke against "
            f"'{parsed.dbname}'. Use disposable *_test database name containing "
            "one of: scratch, tmp, disposable."
        )


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


def truncate_public_tables(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
            table_names = [row[0] for row in cur.fetchall()]
            if not table_names:
                return

            qualified_tables = sql.SQL(", ").join(
                sql.SQL("{}.{}").format(sql.Identifier("public"), sql.Identifier(name))
                for name in table_names
            )
            cur.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(qualified_tables)
            )


def table_exists(database_url: str, table_name: str) -> bool:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            row = cur.fetchone()
            return row is not None and row[0] is not None
