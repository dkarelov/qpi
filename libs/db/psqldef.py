from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

PsqlDefMode = Literal["apply", "dry-run", "export"]


@dataclass(frozen=True)
class PostgresTarget:
    host: str
    port: int
    user: str
    dbname: str
    password: str | None


def normalize_database_url(database_url: str) -> str:
    normalized = database_url.strip()
    if normalized.startswith("postgresql+psycopg://"):
        return "postgresql://" + normalized[len("postgresql+psycopg://") :]
    if normalized.startswith("postgres://"):
        return "postgresql://" + normalized[len("postgres://") :]
    return normalized


def parse_database_url(database_url: str) -> PostgresTarget:
    normalized = normalize_database_url(database_url)
    parsed = urlparse(normalized)

    if parsed.scheme != "postgresql":
        raise ValueError("DATABASE_URL must use postgresql:// scheme")

    dbname = parsed.path.lstrip("/")
    if not dbname or "/" in dbname:
        raise ValueError("DATABASE_URL must include a valid database name")

    user = unquote(parsed.username) if parsed.username else "postgres"
    password = unquote(parsed.password) if parsed.password else None

    return PostgresTarget(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 5432,
        user=user,
        dbname=dbname,
        password=password,
    )


def build_psqldef_command(
    target: PostgresTarget,
    *,
    mode: PsqlDefMode,
    schema_file: Path | None = None,
    enable_drop: bool = False,
) -> list[str]:
    if mode in {"apply", "dry-run"} and schema_file is None:
        raise ValueError("schema_file is required for apply/dry-run")

    command = [
        "psqldef",
        "-h",
        target.host,
        "-p",
        str(target.port),
        "-U",
        target.user,
    ]

    if schema_file is not None:
        command.extend(["--file", str(schema_file)])

    if enable_drop:
        command.append("--enable-drop")

    command.append(target.dbname)

    if mode == "apply":
        command.append("--apply")
    elif mode == "dry-run":
        command.append("--dry-run")
    elif mode == "export":
        command.append("--export")
    else:
        raise ValueError(f"unsupported psqldef mode: {mode}")

    return command


def run_psqldef(
    database_url: str,
    *,
    mode: PsqlDefMode,
    schema_file: Path | None = None,
    enable_drop: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    if schema_file is not None and not schema_file.exists():
        raise FileNotFoundError(f"schema file not found: {schema_file}")

    target = parse_database_url(database_url)
    command = build_psqldef_command(
        target,
        mode=mode,
        schema_file=schema_file,
        enable_drop=enable_drop,
    )

    env = os.environ.copy()
    if target.password:
        env["PGPASSWORD"] = target.password

    return subprocess.run(
        command,
        check=True,
        env=env,
        text=True,
        capture_output=capture_output,
    )
