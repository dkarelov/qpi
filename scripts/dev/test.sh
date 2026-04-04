#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lockfile="${QPI_TEST_DB_LOCKFILE:-/tmp/qpi-test-db.lock}"
db_test_timeout_seconds="${QPI_DB_TEST_TIMEOUT_SECONDS:-900}"
test_env_file="${QPI_TEST_ENV_FILE:-${repo_root}/.env.test.local}"

usage() {
  cat <<'EOF' >&2
usage:
  test.sh fast
  test.sh doctor
  test.sh integration|schema-compat|migration-smoke|all
  test.sh affected --base <sha> --head <sha>
  test.sh affected --paths <path> [<path> ...]
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

command_name="$1"
shift

run_pytest() {
  # shellcheck disable=SC2030,SC2031
  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
    uv run pytest -q -s -p pytest_asyncio.plugin "$@"
  )
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

run_pytest_with_timeout() {
  local context="$1"
  shift

  local status=0
  # shellcheck disable=SC2030,SC2031
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

load_test_env_if_present() {
  if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
    return
  fi
  if [[ ! -f "${test_env_file}" ]]; then
    return
  fi
  # shellcheck disable=SC1090
  set -a && source "${test_env_file}" && set +a
}

test_database_mode() {
  TEST_DATABASE_URL="${TEST_DATABASE_URL:-}" python3 - <<'PY'
from urllib.parse import urlparse
import os

url = os.environ.get("TEST_DATABASE_URL", "").strip()
parsed = urlparse(url)
host = parsed.hostname or ""
port = parsed.port or 5432
mode = "tunnel" if host == "127.0.0.1" and port == 15432 else "private"
print(mode)
PY
}

local_tunnel_listener_present() {
  ss -ltnp | rg ':15432\b' >/dev/null 2>&1
}

ensure_local_tunnel_if_needed() {
  if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
    return
  fi
  if [[ "${QPI_AUTO_START_TEST_DB_TUNNEL:-1}" != "1" ]]; then
    return
  fi
  if [[ "$(test_database_mode)" != "tunnel" ]]; then
    return
  fi
  if local_tunnel_listener_present; then
    return
  fi
  if [[ -z "${QPI_DB_VM_HOST:-}" || -z "${QPI_DB_VM_SSH_PROXY_HOST:-}" ]]; then
    return
  fi

  local ssh_key_path="${QPI_DB_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
  local proxy_user="${QPI_DB_VM_SSH_PROXY_USER:-ubuntu}"
  local proxy_port="${QPI_DB_VM_SSH_PROXY_PORT:-22}"

  if [[ ! -f "${ssh_key_path}" ]]; then
    return
  fi

  echo "Auto-starting local DB tunnel to ${QPI_DB_VM_HOST}:5432 via ${QPI_DB_VM_SSH_PROXY_HOST}" >&2
  ssh -fNT \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    -p "${proxy_port}" \
    -i "${ssh_key_path}" \
    -L 127.0.0.1:15432:"${QPI_DB_VM_HOST}":5432 \
    "${proxy_user}@${QPI_DB_VM_SSH_PROXY_HOST}" || true
}

run_doctor() {
  "${repo_root}/scripts/dev/test_doctor.sh"
}

resolve_selection_from_paths() {
  # shellcheck disable=SC2030,SC2031
  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    python3 -m libs.devtools.validation_selection \
      --repo-root "${repo_root}" \
      --format shell \
      --paths "$@"
  )
}

choose_reset_script() {
  if [[ -n "${QPI_DB_VM_HOST:-}" && -z "${TEST_DATABASE_ADMIN_URL:-}" ]]; then
    printf '%s\n' "${repo_root}/scripts/dev/reset_remote_test_dbs.sh"
    return
  fi
  printf '%s\n' "${repo_root}/scripts/dev/reset_test_db.sh"
}

run_fast() {
  assert_manifest_coverage
  mapfile -t fast_tests < <(collect_fast_tests)
  run_pytest "${fast_tests[@]}"
}

run_manifest_locally() {
  local kind="$1"
  load_test_env_if_present
  ensure_local_tunnel_if_needed

  local reset_script
  reset_script="$(choose_reset_script)"

  require_test_database "${kind}"
  run_doctor

  export QPI_SKIP_TEST_DB_LOCK=1
  mapfile -t files < <(load_manifest "${kind}")
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo "No files found for manifest: ${kind}" >&2
    exit 1
  fi

  "${reset_script}"
  case "${kind}" in
    migration-smoke)
      RUN_MIGRATION_SMOKE=1 run_pytest_with_timeout "${kind}" "${files[@]}"
      ;;
    *)
      run_pytest_with_timeout "${kind}" "${files[@]}"
      ;;
  esac
}

run_targeted_locally() {
  load_test_env_if_present
  ensure_local_tunnel_if_needed

  local reset_script
  reset_script="$(choose_reset_script)"

  require_test_database "targeted"
  run_doctor

  export QPI_SKIP_TEST_DB_LOCK=1
  "${reset_script}"
  run_pytest_with_timeout "targeted" "$@"
}

run_private_runner_manifest() {
  "${repo_root}/scripts/dev/run_db_tests_on_runner.sh" "$@"
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

run_full_local_validation() {
  run_manifest_locally integration
  run_manifest_locally schema-compat
  if [[ "${1:-false}" == "true" ]]; then
    run_manifest_locally migration-smoke
  fi
}

collect_changed_paths_from_git() {
  local base_sha="$1"
  local head_sha="$2"
  (
    cd "${repo_root}"
    git diff --name-only "${base_sha}" "${head_sha}"
  )
}

run_affected() {
  local base_sha=""
  local head_sha=""
  local use_paths=0
  local -a explicit_paths=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base)
        base_sha="${2:-}"
        shift 2
        ;;
      --head)
        head_sha="${2:-}"
        shift 2
        ;;
      --paths)
        use_paths=1
        shift
        while [[ $# -gt 0 ]]; do
          explicit_paths+=("$1")
          shift
        done
        ;;
      *)
        echo "Unknown affected option: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  if [[ "${use_paths}" -eq 1 && ( -n "${base_sha}" || -n "${head_sha}" ) ]]; then
    echo "Use either --paths or --base/--head for affected mode." >&2
    exit 1
  fi

  local -a changed_paths=()
  if [[ "${use_paths}" -eq 1 ]]; then
    if [[ "${#explicit_paths[@]}" -eq 0 ]]; then
      echo "affected --paths requires at least one path." >&2
      exit 1
    fi
    changed_paths=("${explicit_paths[@]}")
  else
    if [[ -z "${base_sha}" || -z "${head_sha}" ]]; then
      echo "affected requires either --paths or both --base and --head." >&2
      exit 1
    fi
    mapfile -t changed_paths < <(collect_changed_paths_from_git "${base_sha}" "${head_sha}")
  fi

  if [[ "${#changed_paths[@]}" -eq 0 ]]; then
    echo "No changed paths resolved for affected mode."
    return
  fi

  selected_groups=""
  fast_pytest_targets=""
  db_pytest_targets=""
  full_db_validation="false"
  db_validation_mode="none"
  requires_migration="false"
  function_targets=""
  eval "$(resolve_selection_from_paths "${changed_paths[@]}")"

  local -a fast_targets=()
  local -a db_targets=()
  if [[ -n "${fast_pytest_targets}" ]]; then
    read -r -a fast_targets <<< "${fast_pytest_targets}"
  fi
  if [[ -n "${db_pytest_targets}" ]]; then
    read -r -a db_targets <<< "${db_pytest_targets}"
  fi

  echo "Resolved affected validation"
  printf 'Changed paths: %s\n' "${changed_paths[*]}"
  echo "Selected groups: ${selected_groups:-<none>}"
  echo "DB validation mode: ${db_validation_mode}"
  echo "Fast targets: ${fast_pytest_targets:-<none>}"
  echo "DB targets: ${db_pytest_targets:-<none>}"
  echo "Function targets: ${function_targets:-<none>}"

  assert_manifest_coverage

  if [[ "${#fast_targets[@]}" -gt 0 ]]; then
    run_pytest "${fast_targets[@]}"
  fi

  if [[ "${full_db_validation}" == "true" ]]; then
    if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
      run_private_runner_manifest integration
      run_private_runner_manifest schema-compat
      if [[ "${requires_migration}" == "true" ]]; then
        run_private_runner_manifest migration-smoke
      fi
      return
    fi
    with_shared_db_lock run_full_local_validation "${requires_migration}"
    return
  fi

  if [[ "${#db_targets[@]}" -gt 0 ]]; then
    if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
      run_private_runner_manifest targeted "${db_targets[@]}"
      return
    fi
    with_shared_db_lock run_targeted_locally "${db_targets[@]}"
    return
  fi

  echo "No DB-backed validation targets resolved."
}

case "${command_name}" in
  fast)
    run_fast
    ;;
  doctor)
    run_doctor
    ;;
  affected)
    run_affected "$@"
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
    if [[ "${QPI_USE_PRIVATE_RUNNER:-0}" == "1" ]]; then
      run_private_runner_manifest integration
      run_private_runner_manifest schema-compat
      run_private_runner_manifest migration-smoke
      exit 0
    fi
    with_shared_db_lock run_full_local_validation true
    ;;
  *)
    usage
    exit 1
    ;;
esac
