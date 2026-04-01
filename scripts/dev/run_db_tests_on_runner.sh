#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
db_test_timeout_seconds="${QPI_DB_TEST_TIMEOUT_SECONDS:-900}"

usage() {
  cat <<'EOF' >&2
usage:
  run_db_tests_on_runner.sh integration|schema-compat|migration-smoke|all
  run_db_tests_on_runner.sh targeted <pytest-file> [<pytest-file> ...]
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

command_name="$1"
shift

run_pytest_with_timeout() {
  local context="$1"
  shift

  local status=0
  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
    timeout --foreground "${db_test_timeout_seconds}" \
      uv run pytest -q -s -p pytest_asyncio.plugin "$@"
  ) || status=$?

  if [[ "${status}" -eq 124 ]]; then
    dump_db_diagnostics "${context}" "$@"
  fi

  return "${status}"
}

dump_db_diagnostics() {
  local context="$1"
  shift

  echo "DB-backed validation timed out during ${context}." >&2
  if [[ "$#" -gt 0 ]]; then
    printf 'Targets: %s\n' "$*" >&2
  fi

  if [[ -z "${TEST_DATABASE_URL:-}" ]] || ! command -v psql >/dev/null 2>&1; then
    return
  fi

  echo "==> pg_stat_activity" >&2
  timeout 10 psql "${TEST_DATABASE_URL}" -v ON_ERROR_STOP=1 -P pager=off <<'SQL' >&2 || true
SELECT
  pid,
  state,
  wait_event_type,
  wait_event,
  now() - query_start AS running_for,
  left(query, 160) AS query
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY query_start NULLS LAST, pid;
SQL

  echo "==> waiting_locks" >&2
  timeout 10 psql "${TEST_DATABASE_URL}" -v ON_ERROR_STOP=1 -P pager=off <<'SQL' >&2 || true
SELECT
  blocked.pid AS blocked_pid,
  blocking.pid AS blocking_pid,
  blocked.wait_event_type,
  blocked.wait_event,
  left(blocked.query, 120) AS blocked_query,
  left(blocking.query, 120) AS blocking_query
FROM pg_locks blocked_lock
JOIN pg_stat_activity blocked ON blocked.pid = blocked_lock.pid
JOIN pg_locks blocking_lock
  ON blocking_lock.locktype = blocked_lock.locktype
 AND blocking_lock.database IS NOT DISTINCT FROM blocked_lock.database
 AND blocking_lock.relation IS NOT DISTINCT FROM blocked_lock.relation
 AND blocking_lock.page IS NOT DISTINCT FROM blocked_lock.page
 AND blocking_lock.tuple IS NOT DISTINCT FROM blocked_lock.tuple
 AND blocking_lock.virtualxid IS NOT DISTINCT FROM blocked_lock.virtualxid
 AND blocking_lock.transactionid IS NOT DISTINCT FROM blocked_lock.transactionid
 AND blocking_lock.classid IS NOT DISTINCT FROM blocked_lock.classid
 AND blocking_lock.objid IS NOT DISTINCT FROM blocked_lock.objid
 AND blocking_lock.objsubid IS NOT DISTINCT FROM blocked_lock.objsubid
 AND blocking_lock.pid <> blocked_lock.pid
JOIN pg_stat_activity blocking ON blocking.pid = blocking_lock.pid
WHERE NOT blocked_lock.granted
  AND blocking_lock.granted
ORDER BY blocked.pid, blocking.pid;
SQL
}

manifest_path() {
  case "$1" in
    integration)
      printf '%s\n' "${repo_root}/tests/db_integration_manifest.txt"
      ;;
    schema-compat)
      printf '%s\n' "${repo_root}/tests/schema_compat_manifest.txt"
      ;;
    migration-smoke)
      printf '%s\n' "${repo_root}/tests/migration_smoke_manifest.txt"
      ;;
    *)
      echo "Unknown manifest kind: $1" >&2
      exit 1
      ;;
  esac
}

load_manifest() {
  sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "$(manifest_path "$1")"
}

reset_remote_databases() {
  "${repo_root}/scripts/dev/reset_remote_test_dbs.sh"
}

run_manifest() {
  local kind="$1"
  mapfile -t files < <(load_manifest "${kind}")
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo "No files found for manifest: ${kind}" >&2
    exit 1
  fi

  echo
  echo "==> ${kind}: ${files[*]}"
  reset_remote_databases

  if [[ "${kind}" == "migration-smoke" ]]; then
    RUN_MIGRATION_SMOKE=1 run_pytest_with_timeout "${kind}" "${files[@]}"
    return
  fi
  run_pytest_with_timeout "${kind}" "${files[@]}"
}

run_targeted() {
  if [[ "$#" -lt 1 ]]; then
    echo "targeted mode requires at least one pytest file." >&2
    exit 1
  fi

  echo
  echo "==> targeted: $*"
  reset_remote_databases
  run_pytest_with_timeout "targeted" "$@"
}

case "${command_name}" in
  integration)
    run_manifest integration
    ;;
  schema-compat)
    run_manifest schema-compat
    ;;
  migration-smoke)
    run_manifest migration-smoke
    ;;
  all)
    run_manifest integration
    run_manifest schema-compat
    run_manifest migration-smoke
    ;;
  targeted)
    run_targeted "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac
