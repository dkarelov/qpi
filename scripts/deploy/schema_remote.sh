#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
usage:
  schema_remote.sh apply
  schema_remote.sh cleanup-plan
  schema_remote.sh cleanup-apply
  schema_remote.sh assert-clean

Required environment:
  BOT_VM_HOST

Optional environment:
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  BOT_DB_TUNNEL_LOCAL_PORT (default: auto)
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

command_name="$1"
case "${command_name}" in
  apply|cleanup-plan|cleanup-apply|assert-clean)
    ;;
  *)
    usage
    exit 1
    ;;
esac

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required." >&2
    exit 1
  fi
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

prepare_ssh_key() {
  local key_source
  if [[ -n "${BOT_VM_SSH_PRIVATE_KEY:-}" ]]; then
    ssh_key_path="$(mktemp)"
    chmod 600 "${ssh_key_path}"
    printf '%s' "${BOT_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      printf '%b' "${BOT_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    fi
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      if ! printf '%s' "${BOT_VM_SSH_PRIVATE_KEY}" | base64 -d > "${ssh_key_path}" 2>/dev/null; then
        :
      fi
    fi
    sed -i 's/\r$//' "${ssh_key_path}"
    generated_ssh_key=1
  else
    key_source="${BOT_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    ssh_key_path="${key_source}"
    generated_ssh_key=0
  fi

  if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
    echo "Failed to decode BOT_VM_SSH_PRIVATE_KEY into a valid private key." >&2
    exit 1
  fi
}

cleanup() {
  if [[ -n "${ssh_control_socket:-}" && -S "${ssh_control_socket}" ]]; then
    ssh \
      -S "${ssh_control_socket}" \
      -p "${BOT_VM_SSH_PORT}" \
      -i "${ssh_key_path}" \
      -o StrictHostKeyChecking=accept-new \
      -O exit \
      "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" >/dev/null 2>&1 || true
  fi
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
  if [[ -n "${ssh_control_socket:-}" ]]; then
    rm -f "${ssh_control_socket}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

fetch_remote_database_url() {
  ssh \
    -p "${BOT_VM_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    "awk 'index(\$0, \"DATABASE_URL=\") == 1 { sub(/^DATABASE_URL=/, \"\"); print; exit }' /etc/qpi/bot.env"
}

resolve_local_port() {
  if [[ -n "${BOT_DB_TUNNEL_LOCAL_PORT:-}" ]]; then
    printf '%s\n' "${BOT_DB_TUNNEL_LOCAL_PORT}"
    return
  fi

  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

parse_remote_database_url() {
  REMOTE_DATABASE_URL="${REMOTE_DATABASE_URL}" python3 - <<'PY'
from urllib.parse import urlparse
import os

parsed = urlparse(os.environ["REMOTE_DATABASE_URL"])
host = parsed.hostname or "127.0.0.1"
port = parsed.port or 5432
print(host)
print(port)
PY
}

build_tunneled_database_url() {
  REMOTE_DATABASE_URL="${REMOTE_DATABASE_URL}" BOT_DB_LOCAL_PORT="${BOT_DB_LOCAL_PORT}" python3 - <<'PY'
from urllib.parse import quote, urlparse, urlunparse
import os

parsed = urlparse(os.environ["REMOTE_DATABASE_URL"])
username = quote(parsed.username or "postgres", safe="")
netloc = username
if parsed.password is not None:
    netloc += f":{quote(parsed.password, safe='')}"
netloc += f"@127.0.0.1:{os.environ['BOT_DB_LOCAL_PORT']}"
print(urlunparse(parsed._replace(netloc=netloc)))
PY
}

open_tunnel() {
  ssh_control_socket="$(mktemp -u)"
  ssh \
    -M \
    -S "${ssh_control_socket}" \
    -fNT \
    -p "${BOT_VM_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    -L "127.0.0.1:${BOT_DB_LOCAL_PORT}:${REMOTE_DB_HOST}:${REMOTE_DB_PORT}" \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}"
}

run_schema_command() {
  local mode="$1"
  uv run python -m libs.db.schema_cli "${mode}" --database-url "${TUNNELED_DATABASE_URL}"
}

require_command uv
require_command python3
require_command psqldef
require_env BOT_VM_HOST

BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"

prepare_ssh_key
REMOTE_DATABASE_URL="$(fetch_remote_database_url)"
if [[ -z "${REMOTE_DATABASE_URL}" ]]; then
  echo "DATABASE_URL missing in /etc/qpi/bot.env on ${BOT_VM_HOST}" >&2
  exit 1
fi

mapfile -t remote_db_parts < <(parse_remote_database_url)
REMOTE_DB_HOST="${remote_db_parts[0]}"
REMOTE_DB_PORT="${remote_db_parts[1]}"
BOT_DB_LOCAL_PORT="$(resolve_local_port)"
open_tunnel
TUNNELED_DATABASE_URL="$(build_tunneled_database_url)"

case "${command_name}" in
  apply)
    uv run python -m libs.db.runtime_schema_compat apply --database-url "${TUNNELED_DATABASE_URL}"
    run_schema_command apply
    run_schema_command assert-clean
    ;;
  cleanup-plan)
    run_schema_command cleanup-plan
    ;;
  cleanup-apply)
    uv run python -m libs.db.runtime_schema_compat apply --database-url "${TUNNELED_DATABASE_URL}"
    run_schema_command cleanup-apply
    run_schema_command apply
    run_schema_command assert-clean
    ;;
  assert-clean)
    run_schema_command assert-clean
    ;;
esac
