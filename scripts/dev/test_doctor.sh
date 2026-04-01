#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
env_file="${QPI_TEST_ENV_FILE:-${repo_root}/.env.test.local}"

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

for command_name in uv psql ssh ss rg python3; do
require_command "${command_name}"
done

scheme=""
host=""
port=""
dbname=""
mode=""

if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  if [[ ! -f "${env_file}" ]]; then
    echo "TEST_DATABASE_URL is unset and ${env_file} is missing." >&2
    echo "Run: scripts/dev/write_test_env.sh --mode tunnel && source .env.test.local" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  set -a && source "${env_file}" && set +a
fi

if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL is still unset after loading ${env_file}." >&2
  exit 1
fi

mapfile -t db_context < <(
  TEST_DATABASE_URL="${TEST_DATABASE_URL}" python3 - <<'PY'
from urllib.parse import urlparse
import os

url = os.environ["TEST_DATABASE_URL"].strip()
parsed = urlparse(url)

scheme = parsed.scheme
host = parsed.hostname or ""
port = parsed.port or 5432
dbname = parsed.path.lstrip("/")
mode = "tunnel" if host == "127.0.0.1" and port == 15432 else "private"

print(f"scheme={scheme}")
print(f"host={host}")
print(f"port={port}")
print(f"dbname={dbname}")
print(f"mode={mode}")
PY
)
eval "${db_context[0]}"
eval "${db_context[1]}"
eval "${db_context[2]}"
eval "${db_context[3]}"
eval "${db_context[4]}"

if [[ "${scheme}" != "postgresql" || -z "${dbname}" ]]; then
  echo "TEST_DATABASE_URL must use postgresql:// and include a database name." >&2
  exit 1
fi

if [[ "${mode}" == "tunnel" ]]; then
  if ! ss -ltnp | rg ':15432\b' >/dev/null 2>&1; then
    echo "Local DB tunnel is missing on 127.0.0.1:15432." >&2
    echo "Run the repo tunnel command before DB-backed tests." >&2
    exit 1
  fi
fi

if ! PGPASSWORD="${PGPASSWORD:-}" timeout 10 psql "${TEST_DATABASE_URL}" -v ON_ERROR_STOP=1 -c "select 1" >/dev/null; then
  echo "Failed to connect to TEST_DATABASE_URL with psql." >&2
  exit 1
fi

echo "env_file=${env_file}"
echo "db_mode=${mode}"
echo "db_host=${host}"
echo "db_port=${port}"
echo "db_name=${dbname}"
echo "tunnel_listener=$([[ "${mode}" == "tunnel" ]] && echo ok || echo not_required)"
echo "psql_connectivity=ok"
echo "doctor=ok"
