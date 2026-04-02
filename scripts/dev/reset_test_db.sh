#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lockfile="${QPI_TEST_DB_LOCKFILE:-/tmp/qpi-test-db.lock}"
template_lib="${script_dir}/test_db_template_lib.sh"

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

# shellcheck source=scripts/dev/test_db_template_lib.sh
source "${template_lib}"

fetch_template_comment() {
  psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 -qtAX <<SQL
$(qpi_template_comment_query_sql "${TEMPLATE_DB_NAME}")
SQL
}

rebuild_template_database() {
  local template_comment="$1"

  echo "Refreshing template database ${TEMPLATE_DB_NAME}"
  psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
$(qpi_template_recreate_sql "${TEMPLATE_DB_NAME}" "${TEST_DB_USER}")
SQL

  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.schema_cli apply
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.schema_cli assert-clean
  )

  psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
$(qpi_template_comment_sql "${TEMPLATE_DB_NAME}" "${template_comment}")
SQL
}

clone_databases_from_template() {
  echo "Cloning ${TEST_DB_NAME} and ${SCRATCH_DB_NAME} from ${TEMPLATE_DB_NAME}"
  psql "${ADMIN_DB_URL}" -v ON_ERROR_STOP=1 <<SQL
$(qpi_template_clone_sql "${TEST_DB_NAME}" "${SCRATCH_DB_NAME}" "${TEMPLATE_DB_NAME}" "${TEST_DB_USER}")
SQL
}

reset_databases() {
  local fingerprint
  local template_comment
  local existing_comment

  eval "$(qpi_template_resolve_db_context)"
  fingerprint="$(qpi_template_fingerprint "${repo_root}")"
  template_comment="$(qpi_template_comment_value "${fingerprint}")"
  existing_comment="$(fetch_template_comment || true)"
  existing_comment="${existing_comment//$'\r'/}"

  if [[ "${existing_comment}" != "${template_comment}" ]]; then
    rebuild_template_database "${template_comment}"
  else
    echo "Reusing template database ${TEMPLATE_DB_NAME}"
  fi

  clone_databases_from_template
}

with_shared_db_lock reset_databases
