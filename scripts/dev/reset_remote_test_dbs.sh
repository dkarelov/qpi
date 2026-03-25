#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

resolve_db_context() {
  TEST_DATABASE_URL="${TEST_DATABASE_URL:-}" TEST_SCRATCH_DATABASE_URL="${TEST_SCRATCH_DATABASE_URL:-}" \
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
        "TEST_DATABASE_URL is unset. reset_remote_test_dbs.sh needs a real disposable test DB URL, "
        "typically postgresql://<app-user>:<password>@10.131.0.28:5432/qpi_test on the private-runner path. "
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

print(f"TEST_DB_URL={shlex.quote(test_url)}")
print(f"TEST_DB_NAME={shlex.quote(test_db)}")
print(f"TEST_DB_USER={shlex.quote(test_user)}")
print(f"SCRATCH_DB_URL={shlex.quote(scratch_url)}")
print(f"SCRATCH_DB_NAME={shlex.quote(urlparse(scratch_url).path.lstrip('/'))}")
PY
}

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

quote_ident() {
  printf '"%s"' "${1//\"/\"\"}"
}

require_env "QPI_DB_VM_HOST"
QPI_DB_VM_SSH_USER="${QPI_DB_VM_SSH_USER:-ubuntu}"
QPI_DB_VM_SSH_PORT="${QPI_DB_VM_SSH_PORT:-22}"
QPI_DB_VM_SSH_PROXY_HOST="${QPI_DB_VM_SSH_PROXY_HOST:-}"
QPI_DB_VM_SSH_PROXY_USER="${QPI_DB_VM_SSH_PROXY_USER:-ubuntu}"
QPI_DB_VM_SSH_PROXY_PORT="${QPI_DB_VM_SSH_PROXY_PORT:-22}"

eval "$(resolve_db_context)"
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

test_db_ident="$(quote_ident "${TEST_DB_NAME}")"
test_db_user_ident="$(quote_ident "${TEST_DB_USER}")"
scratch_db_ident="$(quote_ident "${SCRATCH_DB_NAME}")"

ssh \
  -p "${QPI_DB_VM_SSH_PORT}" \
  -i "${ssh_key_path}" \
  -o StrictHostKeyChecking=accept-new \
  "${proxy_ssh_args[@]}" \
  "${QPI_DB_VM_SSH_USER}@${QPI_DB_VM_HOST}" \
  "sudo -u postgres psql -v ON_ERROR_STOP=1 postgres <<'SQL'
DROP DATABASE IF EXISTS ${scratch_db_ident} WITH (FORCE);
DROP DATABASE IF EXISTS ${test_db_ident} WITH (FORCE);
CREATE DATABASE ${test_db_ident} OWNER ${test_db_user_ident};
CREATE DATABASE ${scratch_db_ident} OWNER ${test_db_user_ident};
SQL"

(
  cd "${repo_root}"
  export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
  DATABASE_URL="${TEST_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
  DATABASE_URL="${TEST_DB_URL}" uv run python -m libs.db.schema_cli apply
  DATABASE_URL="${SCRATCH_DB_URL}" uv run python -m libs.db.runtime_schema_compat apply
  DATABASE_URL="${SCRATCH_DB_URL}" uv run python -m libs.db.schema_cli apply
)
