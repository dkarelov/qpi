#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
usage: run_db_tests_on_runner.sh integration|schema-compat|migration-smoke|all
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

run_manifest() {
  local kind="$1"
  local file
  local failures=0

  while IFS= read -r file; do
    [[ -n "${file}" ]] || continue

    echo
    echo "==> ${kind}: ${file}"
    "${repo_root}/scripts/dev/reset_remote_test_dbs.sh"

    if [[ "${kind}" == "migration-smoke" ]]; then
      RUN_MIGRATION_SMOKE=1 run_pytest "${file}" || failures=1
    else
      run_pytest "${file}" || failures=1
    fi

    if [[ "${failures}" -ne 0 ]]; then
      echo "Failed: ${file}" >&2
      exit 1
    fi
  done < <(load_manifest "${kind}")
}

case "$1" in
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
  *)
    usage
    exit 1
    ;;
esac
