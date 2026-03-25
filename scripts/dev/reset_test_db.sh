#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lockfile="${QPI_TEST_DB_LOCKFILE:-/tmp/qpi-test-db.lock}"

resolve_db_context() {
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
        "TEST_DATABASE_URL is unset. reset_test_db.sh needs a real disposable test DB URL, "
        "typically postgresql://<app-user>:<password>@127.0.0.1:15432/qpi_test via the local tunnel. "
        "Do not invent credentials."
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

print(f"TEST_DB_URL={shlex.quote(test_url)}")
print(f"TEST_DB_NAME={shlex.quote(test_db)}")
print(f"TEST_DB_USER={shlex.quote(test_user)}")
print(f"SCRATCH_DB_URL={shlex.quote(scratch_url)}")
print(f"SCRATCH_DB_NAME={shlex.quote(urlparse(scratch_url).path.lstrip('/'))}")
print(f"ADMIN_DB_URL={shlex.quote(admin_url)}")
PY
}

with_shared_db_lock() {
  if [[ "${QPI_SKIP_TEST_DB_LOCK:-0}" == "1" ]]; then
    "$@"
    return
  fi

  exec 9>"${lockfile}"
  if ! flock -n 9; then
    echo "Another shared-db test run already holds ${lockfile}." >&2
    echo "Reset aborted to avoid clobbering an active run." >&2
    exit 1
  fi

  "$@"
}

quote_ident() {
  printf '"%s"' "${1//\"/\"\"}"
}

reset_databases() {
  eval "$(resolve_db_context)"

  local test_db_ident
  local test_db_user_ident
  local scratch_db_ident
  test_db_ident="$(quote_ident "${TEST_DB_NAME}")"
  test_db_user_ident="$(quote_ident "${TEST_DB_USER}")"
  scratch_db_ident="$(quote_ident "${SCRATCH_DB_NAME}")"

  psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS ${scratch_db_ident} WITH (FORCE);
DROP DATABASE IF EXISTS ${test_db_ident} WITH (FORCE);
CREATE DATABASE ${test_db_ident} OWNER ${test_db_user_ident};
CREATE DATABASE ${scratch_db_ident} OWNER ${test_db_user_ident};
SQL

  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    DATABASE_URL="${TEST_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
    DATABASE_URL="${TEST_DB_URL}" uv run python -m libs.db.schema_cli apply
    DATABASE_URL="${SCRATCH_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
    DATABASE_URL="${SCRATCH_DB_URL}" uv run python -m libs.db.schema_cli apply
  )
}

with_shared_db_lock reset_databases
