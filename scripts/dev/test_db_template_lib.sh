#!/usr/bin/env bash
# shellcheck shell=bash

qpi_template_resolve_db_context() {
  TEST_DATABASE_URL="${TEST_DATABASE_URL:-}" TEST_SCRATCH_DATABASE_URL="${TEST_SCRATCH_DATABASE_URL:-}" \
    TEST_DATABASE_ADMIN_URL="${TEST_DATABASE_ADMIN_URL:-}" \
    python3 - <<'PY'
import os
import shlex
from urllib.parse import urlparse, urlunparse


def normalize(url: str) -> str:
    value = url.strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    if value.startswith("postgresql+psycopg://"):
        value = "postgresql://" + value[len("postgresql+psycopg://") :]
    return value


def replace_db(url: str, dbname: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{dbname}"))


test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
if not test_url:
    raise SystemExit(
        "TEST_DATABASE_URL is unset. A real disposable database URL is required for DB-backed validation."
    )

test_url = normalize(test_url)
parsed = urlparse(test_url)
if parsed.scheme != "postgresql" or not parsed.path.lstrip("/"):
    raise SystemExit("TEST_DATABASE_URL must use the postgresql:// scheme and include a database name.")
if not parsed.username:
    raise SystemExit("TEST_DATABASE_URL must include a database user.")

test_db = parsed.path.lstrip("/")
test_user = parsed.username
scratch_url = os.environ.get("TEST_SCRATCH_DATABASE_URL", "").strip()
scratch_url = normalize(scratch_url) if scratch_url else replace_db(test_url, f"{test_db}_scratch")
admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL", "").strip()
admin_url = normalize(admin_url) if admin_url else replace_db(test_url, "postgres")
template_db = f"{test_db}_template"
template_url = replace_db(test_url, template_db)

print(f"TEST_DB_URL={shlex.quote(test_url)}")
print(f"TEST_DB_NAME={shlex.quote(test_db)}")
print(f"TEST_DB_USER={shlex.quote(test_user)}")
print(f"SCRATCH_DB_URL={shlex.quote(scratch_url)}")
print(f"SCRATCH_DB_NAME={shlex.quote(urlparse(scratch_url).path.lstrip('/'))}")
print(f"ADMIN_DB_URL={shlex.quote(admin_url)}")
print(f"TEMPLATE_DB_URL={shlex.quote(template_url)}")
print(f"TEMPLATE_DB_NAME={shlex.quote(template_db)}")
PY
}

qpi_template_fingerprint() {
  local repo_root="$1"
  python3 - "$repo_root" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
patterns = (
    "schema/schema.sql",
    "libs/db/**/*.py",
    "scripts/dev/test_db_template_lib.sh",
    "scripts/dev/reset_test_db.sh",
    "scripts/dev/reset_remote_test_dbs.sh",
)
paths: set[pathlib.Path] = set()
for pattern in patterns:
    for path in root.glob(pattern):
        if path.is_file():
            paths.add(path.resolve())

if not paths:
    raise SystemExit("No template fingerprint inputs were resolved.")

digest = hashlib.sha256()
for path in sorted(paths):
    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

qpi_template_quote_ident() {
  printf '"%s"' "${1//\"/\"\"}"
}

qpi_template_sql_literal() {
  printf "'%s'" "${1//\'/\'\'}"
}

qpi_template_comment_value() {
  printf 'qpi-template-fingerprint:%s\n' "$1"
}

qpi_template_comment_query_sql() {
  cat <<SQL
SELECT COALESCE(shobj_description(oid, 'pg_database'), '')
FROM pg_database
WHERE datname = $(qpi_template_sql_literal "$1");
SQL
}

qpi_template_recreate_sql() {
  local template_ident
  local owner_ident
  template_ident="$(qpi_template_quote_ident "$1")"
  owner_ident="$(qpi_template_quote_ident "$2")"
  cat <<SQL
DROP DATABASE IF EXISTS ${template_ident} WITH (FORCE);
CREATE DATABASE ${template_ident} OWNER ${owner_ident};
SQL
}

qpi_template_clone_sql() {
  local test_ident
  local scratch_ident
  local template_ident
  local owner_ident

  test_ident="$(qpi_template_quote_ident "$1")"
  scratch_ident="$(qpi_template_quote_ident "$2")"
  template_ident="$(qpi_template_quote_ident "$3")"
  owner_ident="$(qpi_template_quote_ident "$4")"

  cat <<SQL
DROP DATABASE IF EXISTS ${scratch_ident} WITH (FORCE);
DROP DATABASE IF EXISTS ${test_ident} WITH (FORCE);
CREATE DATABASE ${test_ident} WITH TEMPLATE ${template_ident} OWNER ${owner_ident};
CREATE DATABASE ${scratch_ident} WITH TEMPLATE ${template_ident} OWNER ${owner_ident};
SQL
}

qpi_template_comment_sql() {
  local template_ident
  template_ident="$(qpi_template_quote_ident "$1")"
  printf 'COMMENT ON DATABASE %s IS %s;\n' \
    "${template_ident}" \
    "$(qpi_template_sql_literal "$2")"
}
