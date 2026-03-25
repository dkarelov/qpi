#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lockfile="${QPI_TEST_DB_LOCKFILE:-/tmp/qpi-test-db.lock}"

usage() {
  cat <<'EOF' >&2
usage: test.sh fast|integration|schema-compat|migration-smoke|all
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
    export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
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
  local path
  path="$(manifest_path "$1")"
  if [[ ! -f "${path}" ]]; then
    echo "Manifest not found: ${path}" >&2
    exit 1
  fi

  sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "${path}"
}

collect_db_tests() {
  {
    load_manifest integration
    load_manifest schema-compat
    load_manifest migration-smoke
  } | sort -u
}

discover_db_backed_tests() {
  (
    cd "${repo_root}"
    if command -v rg >/dev/null 2>&1; then
      rg -l '\b(db_pool|isolated_database|prepared_database|test_database_url|migration_smoke_database_url)\b' \
        tests/test_*.py | sort
    else
      grep -lE '\b(db_pool|isolated_database|prepared_database|test_database_url|migration_smoke_database_url)\b' \
        tests/test_*.py | sort
    fi
  )
}

assert_manifest_coverage() {
  mapfile -t discovered_db_tests < <(discover_db_backed_tests)
  mapfile -t listed_db_tests < <(collect_db_tests)

  local discovered listed
  discovered="$(printf '%s\n' "${discovered_db_tests[@]}")"
  listed="$(printf '%s\n' "${listed_db_tests[@]}")"

  if [[ "${discovered}" != "${listed}" ]]; then
    echo "DB test manifest mismatch." >&2
    echo "Discovered DB-backed tests:" >&2
    printf '%s\n' "${discovered_db_tests[@]}" >&2
    echo >&2
    echo "Manifest-listed DB-backed tests:" >&2
    printf '%s\n' "${listed_db_tests[@]}" >&2
    exit 1
  fi
}

collect_fast_tests() {
  mapfile -t all_tests < <(cd "${repo_root}" && find tests -maxdepth 1 -name 'test_*.py' | sort)
  mapfile -t db_tests < <(collect_db_tests)

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

require_test_database() {
  if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
    echo "TEST_DATABASE_URL is unset." >&2
    echo "This is normal in a plain local shell; only DB-backed suites require it." >&2
    echo "Use scripts/dev/test.sh fast for the non-DB suite, or export a real disposable DB URL before running ${1}." >&2
    echo "Accepted patterns: postgresql://<app-user>:<password>@127.0.0.1:15432/qpi_test or postgresql://<app-user>:<password>@10.131.0.28:5432/qpi_test." >&2
    echo "Do not invent credentials." >&2
    exit 1
  fi
}

run_fast() {
  assert_manifest_coverage
  mapfile -t fast_tests < <(collect_fast_tests)
  run_pytest "${fast_tests[@]}"
}

run_manifest_locally() {
  local kind="$1"
  local file
  local reset_script="${repo_root}/scripts/dev/reset_test_db.sh"

  require_test_database "${kind}"
  export QPI_SKIP_TEST_DB_LOCK=1

  if [[ -n "${QPI_DB_VM_HOST:-}" && -z "${TEST_DATABASE_ADMIN_URL:-}" ]]; then
    reset_script="${repo_root}/scripts/dev/reset_remote_test_dbs.sh"
  fi

  while IFS= read -r file; do
    [[ -n "${file}" ]] || continue
    "${reset_script}"
    case "${kind}" in
      migration-smoke)
        RUN_MIGRATION_SMOKE=1 run_pytest "${file}"
        ;;
      *)
        run_pytest "${file}"
        ;;
    esac
  done < <(load_manifest "${kind}")
}

run_private_runner_manifest() {
  "${repo_root}/scripts/dev/run_db_tests_on_runner.sh" "$1"
}

run_integration() {
  if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
    run_private_runner_manifest integration
    return
  fi
  run_manifest_locally integration
}

run_schema_compat() {
  if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
    run_private_runner_manifest schema-compat
    return
  fi
  run_manifest_locally schema-compat
}

run_migration_smoke() {
  if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
    run_private_runner_manifest migration-smoke
    return
  fi
  run_manifest_locally migration-smoke
}

case "$1" in
  fast)
    run_fast
    ;;
  integration)
    with_shared_db_lock run_integration
    ;;
  schema-compat)
    with_shared_db_lock run_schema_compat
    ;;
  migration-smoke)
    with_shared_db_lock run_migration_smoke
    ;;
  all)
    run_fast
    with_shared_db_lock run_integration
    with_shared_db_lock run_schema_compat
    with_shared_db_lock run_migration_smoke
    ;;
  *)
    usage
    exit 1
    ;;
esac
