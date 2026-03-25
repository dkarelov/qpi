#!/usr/bin/env bash
set -euo pipefail

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
    raise SystemExit("TEST_DATABASE_URL must point at qpi_test before kill-stuck-tests.sh can run.")

test_url = normalize(test_url)
parsed = urlparse(test_url)
if parsed.scheme != "postgresql" or not parsed.path.lstrip("/"):
    raise SystemExit("TEST_DATABASE_URL must use the postgresql:// scheme and include a database name.")

test_db = parsed.path.lstrip("/")
scratch_url = os.environ.get("TEST_SCRATCH_DATABASE_URL", "").strip()
scratch_url = normalize(scratch_url) if scratch_url else replace_db(test_url, f"{test_db}_scratch")
admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL", "").strip()
admin_url = normalize(admin_url) if admin_url else replace_db(test_url, "postgres")

print(f"TEST_DB_NAME={shlex.quote(test_db)}")
print(f"SCRATCH_DB_NAME={shlex.quote(urlparse(scratch_url).path.lstrip('/'))}")
print(f"ADMIN_DB_URL={shlex.quote(admin_url)}")
PY
}

eval "$(resolve_db_context)"

echo "Current test-db sessions:"
psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
SELECT
  pid,
  datname,
  usename,
  application_name,
  state,
  wait_event_type,
  wait_event
FROM pg_stat_activity
WHERE datname IN ('${TEST_DB_NAME}', '${SCRATCH_DB_NAME}')
  AND pid <> pg_backend_pid()
ORDER BY datname, pid;
SQL

echo
echo "Terminating lingering test-db sessions..."
psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname IN ('${TEST_DB_NAME}', '${SCRATCH_DB_NAME}')
  AND pid <> pg_backend_pid();
SQL
