from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import psycopg

from libs.db.psqldef import run_psqldef

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_FILE = PROJECT_ROOT / "schema" / "schema.sql"
DEFAULT_EMPTY_SCHEMA_FILE = PROJECT_ROOT / "schema" / "empty.sql"
_PSQLDEF_CLEAN_OUTPUT = "-- Nothing is modified --"


def _resolve_database_url(explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url

    for env_name in ("DATABASE_URL", "TEST_DATABASE_URL"):
        value = os.getenv(env_name)
        if value:
            return value

    raise ValueError("DATABASE_URL (or TEST_DATABASE_URL) must be set")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage QPI schema with psqldef")
    parser.add_argument(
        "command",
        choices=["plan", "apply", "cleanup-plan", "cleanup-apply", "assert-clean", "drop", "export"],
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--schema-file", type=Path, default=DEFAULT_SCHEMA_FILE)
    parser.add_argument("--empty-schema-file", type=Path, default=DEFAULT_EMPTY_SCHEMA_FILE)
    parser.add_argument("--out", type=Path, default=None)
    return parser


def _drop_public_schema(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")


def _run_cleanup_plan(
    database_url: str,
    *,
    schema_file: Path,
) -> subprocess.CompletedProcess[str]:
    return run_psqldef(
        database_url,
        mode="dry-run",
        schema_file=schema_file,
        enable_drop=True,
        capture_output=True,
    )


def _cleanup_plan_is_clean(output: str) -> bool:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines == [_PSQLDEF_CLEAN_OUTPUT]


def cli(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        database_url = _resolve_database_url(args.database_url)

        if args.command == "plan":
            run_psqldef(database_url, mode="dry-run", schema_file=args.schema_file)
            return 0

        if args.command == "apply":
            run_psqldef(database_url, mode="apply", schema_file=args.schema_file)
            return 0

        if args.command == "cleanup-plan":
            cleanup_result = _run_cleanup_plan(database_url, schema_file=args.schema_file)
            sys.stdout.write(cleanup_result.stdout)
            return 0

        if args.command == "cleanup-apply":
            run_psqldef(
                database_url,
                mode="apply",
                schema_file=args.schema_file,
                enable_drop=True,
            )
            return 0

        if args.command == "assert-clean":
            cleanup_result = _run_cleanup_plan(database_url, schema_file=args.schema_file)
            sys.stdout.write(cleanup_result.stdout)
            if _cleanup_plan_is_clean(cleanup_result.stdout):
                return 0
            print(
                "Schema drift detected. Run `python -m libs.db.schema_cli cleanup-apply` first.",
                file=sys.stderr,
            )
            return 3

        if args.command == "drop":
            _drop_public_schema(database_url)
            return 0

        export_result = run_psqldef(database_url, mode="export", capture_output=True)
        if args.out:
            args.out.write_text(export_result.stdout)
        else:
            sys.stdout.write(export_result.stdout)
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, file=sys.stdout, end="")
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(cli())
