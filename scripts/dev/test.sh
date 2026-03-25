#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lockfile="${QPI_TEST_DB_LOCKFILE:-/tmp/qpi-test-db.lock}"

usage() {
  cat <<'EOF' >&2
usage: test.sh fast|integration|migration-smoke|all
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

run_pytest() {
  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    uv run pytest -q -s -p pytest_asyncio.plugin "$@"
  )
}

with_shared_db_lock() {
  if [[ "${QPI_SKIP_TEST_DB_LOCK:-0}" == "1" ]]; then
    "$@"
    return
  fi

  exec 9>"${lockfile}"
  if ! flock -n 9; then
    echo "Another shared-db test run already holds ${lockfile}." >&2
    echo "Wait for it to finish or run scripts/dev/kill-stuck-tests.sh if a stale session is blocking resets." >&2
    exit 1
  fi

  "$@"
}

require_test_database() {
  if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
    echo "TEST_DATABASE_URL must point at qpi_test before running ${1}." >&2
    exit 1
  fi
}

collect_fast_tests() {
  mapfile -t all_tests < <(cd "${repo_root}" && find tests -maxdepth 1 -name 'test_*.py' | sort)
  mapfile -t db_tests < <(
    cd "${repo_root}" &&
      rg -l '\b(db_pool|isolated_database|prepared_database|test_database_url|migration_smoke_database_url)\b' \
        tests/test_*.py | sort
  )

  local candidate
  local skip
  local fast_tests=()
  for candidate in "${all_tests[@]}"; do
    skip=0
    for db_test in "${db_tests[@]}"; do
      if [[ "${candidate}" == "${db_test}" ]]; then
        skip=1
        break
      fi
    done
    if [[ "${skip}" -eq 0 ]]; then
      fast_tests+=("${candidate}")
    fi
  done

  if [[ "${#fast_tests[@]}" -eq 0 ]]; then
    echo "No fast tests were discovered." >&2
    exit 1
  fi

  printf '%s\n' "${fast_tests[@]}"
}

run_fast() {
  mapfile -t fast_tests < <(collect_fast_tests)
  run_pytest "${fast_tests[@]}"
}

run_integration() {
  require_test_database "integration"
  export QPI_SKIP_TEST_DB_LOCK=1
  "${repo_root}/scripts/dev/reset_test_db.sh"
  run_pytest -m "not migration_smoke"
}

run_migration_smoke() {
  require_test_database "migration-smoke"
  export QPI_SKIP_TEST_DB_LOCK=1
  "${repo_root}/scripts/dev/reset_test_db.sh"
  RUN_MIGRATION_SMOKE=1 run_pytest -m migration_smoke
}

case "$1" in
  fast)
    run_fast
    ;;
  integration)
    with_shared_db_lock run_integration
    ;;
  migration-smoke)
    with_shared_db_lock run_migration_smoke
    ;;
  all)
    run_fast
    with_shared_db_lock run_integration
    with_shared_db_lock run_migration_smoke
    ;;
  *)
    usage
    exit 1
    ;;
esac
