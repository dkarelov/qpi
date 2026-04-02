#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
template_lib="${script_dir}/test_db_template_lib.sh"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required." >&2
    exit 1
  fi
}

proxy_ssh_args=()

prepare_ssh_key() {
  local key_source
  if [[ -n "${QPI_DB_VM_SSH_PRIVATE_KEY:-}" ]]; then
    ssh_key_path="$(mktemp)"
    chmod 600 "${ssh_key_path}"
    printf '%s' "${QPI_DB_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      printf '%b' "${QPI_DB_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    fi
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      if ! printf '%s' "${QPI_DB_VM_SSH_PRIVATE_KEY}" | base64 -d > "${ssh_key_path}" 2>/dev/null; then
        :
      fi
    fi
    sed -i 's/\r$//' "${ssh_key_path}"
    generated_ssh_key=1
  else
    key_source="${QPI_DB_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    ssh_key_path="${key_source}"
    generated_ssh_key=0
  fi

  if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
    echo "Failed to decode QPI_DB_VM_SSH_PRIVATE_KEY into a valid private key." >&2
    exit 1
  fi
}

cleanup() {
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

# shellcheck source=scripts/dev/test_db_template_lib.sh
source "${template_lib}"

run_remote_admin_sql() {
  local psql_flags="${1:-}"
  if [[ $# -gt 0 ]]; then
    shift
  fi
  ssh \
    -p "${QPI_DB_VM_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    "${proxy_ssh_args[@]}" \
    "${QPI_DB_VM_SSH_USER}@${QPI_DB_VM_HOST}" \
    "sudo -u postgres psql -v ON_ERROR_STOP=1 ${psql_flags} postgres" \
    "$@"
}

fetch_remote_template_comment() {
  run_remote_admin_sql "-qtAX" <<SQL
$(qpi_template_comment_query_sql "${TEMPLATE_DB_NAME}")
SQL
}

rebuild_remote_template_database() {
  local template_comment="$1"

  echo "Refreshing template database ${TEMPLATE_DB_NAME} through ${QPI_DB_VM_HOST}"
  run_remote_admin_sql <<SQL
$(qpi_template_recreate_sql "${TEMPLATE_DB_NAME}" "${TEST_DB_USER}")
SQL

  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.schema_cli apply
    DATABASE_URL="${TEMPLATE_DB_URL}" uv run python -m libs.db.schema_cli assert-clean
  )

  run_remote_admin_sql <<SQL
$(qpi_template_comment_sql "${TEMPLATE_DB_NAME}" "${template_comment}")
SQL
}

clone_remote_databases_from_template() {
  echo "Cloning ${TEST_DB_NAME} and ${SCRATCH_DB_NAME} from ${TEMPLATE_DB_NAME}"
  run_remote_admin_sql <<SQL
$(qpi_template_clone_sql "${TEST_DB_NAME}" "${SCRATCH_DB_NAME}" "${TEMPLATE_DB_NAME}" "${TEST_DB_USER}")
SQL
}

require_env "QPI_DB_VM_HOST"
QPI_DB_VM_SSH_USER="${QPI_DB_VM_SSH_USER:-ubuntu}"
QPI_DB_VM_SSH_PORT="${QPI_DB_VM_SSH_PORT:-22}"
QPI_DB_VM_SSH_PROXY_HOST="${QPI_DB_VM_SSH_PROXY_HOST:-}"
QPI_DB_VM_SSH_PROXY_USER="${QPI_DB_VM_SSH_PROXY_USER:-ubuntu}"
QPI_DB_VM_SSH_PROXY_PORT="${QPI_DB_VM_SSH_PROXY_PORT:-22}"

eval "$(qpi_template_resolve_db_context)"
prepare_ssh_key

if [[ -n "${QPI_DB_VM_SSH_PROXY_HOST}" ]]; then
  proxy_command="$(
    printf \
      'ssh -p %q -i %q -o StrictHostKeyChecking=accept-new -W %%h:%%p %q@%q' \
      "${QPI_DB_VM_SSH_PROXY_PORT}" \
      "${ssh_key_path}" \
      "${QPI_DB_VM_SSH_PROXY_USER}" \
      "${QPI_DB_VM_SSH_PROXY_HOST}"
  )"
  proxy_ssh_args=(-o "ProxyCommand=${proxy_command}")
fi

template_fingerprint="$(qpi_template_fingerprint "${repo_root}")"
template_comment="$(qpi_template_comment_value "${template_fingerprint}")"
existing_template_comment="$(fetch_remote_template_comment || true)"
existing_template_comment="${existing_template_comment//$'\r'/}"

if [[ "${existing_template_comment}" != "${template_comment}" ]]; then
  rebuild_remote_template_database "${template_comment}"
else
  echo "Reusing template database ${TEMPLATE_DB_NAME}"
fi

clone_remote_databases_from_template
