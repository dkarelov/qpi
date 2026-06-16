#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF' >&2
usage:
  qpi_pg_mcp.sh install
  qpi_pg_mcp.sh status
  qpi_pg_mcp.sh smoke
  qpi_pg_mcp.sh rotate-secret
  qpi_pg_mcp.sh uninstall

Installs the qpi-pg-prod DBHub MCP wrapper on the bot VM jump host. The MCP
server is launched on demand through SSH stdio; no HTTP listener is created.

Required environment:
  BOT_VM_HOST

Optional environment:
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  QPI_DB_VM_HOST (default: parsed from /etc/qpi/bot.env DATABASE_URL)
  QPI_DB_VM_SSH_USER (default: ubuntu)
  QPI_DB_VM_SSH_PORT (default: 22)
  QPI_DB_VM_SSH_KEY_PATH (default: BOT_VM_SSH_KEY_PATH or ~/.ssh/id_rsa)
  QPI_DB_VM_SSH_PRIVATE_KEY
  QPI_MCP_DB_ROLE (default: qpi_mcp_readonly)
  DBHUB_IMAGE (default: bytebase/dbhub:0.22.3)
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

command_name="$1"
case "${command_name}" in
  install|status|smoke|rotate-secret|uninstall)
    ;;
  *)
    usage
    exit 1
    ;;
esac

BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
QPI_DB_VM_SSH_USER="${QPI_DB_VM_SSH_USER:-ubuntu}"
QPI_DB_VM_SSH_PORT="${QPI_DB_VM_SSH_PORT:-22}"
QPI_MCP_DB_ROLE="${QPI_MCP_DB_ROLE:-qpi_mcp_readonly}"
DBHUB_IMAGE="${DBHUB_IMAGE:-bytebase/dbhub:0.22.3}"

bot_ssh_key_path=""
bot_generated_ssh_key="0"
db_ssh_key_path=""
db_generated_ssh_key="0"
remote_db_host=""
remote_db_port=""
remote_db_name=""
remote_db_user=""

cleanup() {
  if [[ "${bot_generated_ssh_key:-0}" == "1" && -n "${bot_ssh_key_path:-}" && -f "${bot_ssh_key_path}" ]]; then
    rm -f "${bot_ssh_key_path}"
  fi
  if [[ "${db_generated_ssh_key:-0}" == "1" && -n "${db_ssh_key_path:-}" && -f "${db_ssh_key_path}" ]]; then
    rm -f "${db_ssh_key_path}"
  fi
}
trap cleanup EXIT

shell_quote() {
  printf '%q' "$1"
}

ssh_bot() {
  ssh \
    -p "${BOT_VM_SSH_PORT}" \
    -i "${bot_ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    "$@"
}

ssh_db() {
  local proxy_command
  proxy_command="$(
    printf \
      'ssh -p %q -i %q -o StrictHostKeyChecking=accept-new -W %%h:%%p %q@%q' \
      "${BOT_VM_SSH_PORT}" \
      "${bot_ssh_key_path}" \
      "${BOT_VM_SSH_USER}" \
      "${BOT_VM_HOST}"
  )"

  ssh \
    -p "${QPI_DB_VM_SSH_PORT}" \
    -i "${db_ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    -o "ProxyCommand=${proxy_command}" \
    "${QPI_DB_VM_SSH_USER}@${QPI_DB_VM_HOST}" \
    "$@"
}

prepare_keys() {
  qpi_prepare_private_key \
    "BOT_VM_SSH_PRIVATE_KEY" \
    "BOT_VM_SSH_KEY_PATH" \
    "${HOME}/.ssh/id_rsa" \
    "bot_ssh_key_path" \
    "bot_generated_ssh_key"

  if [[ -n "${QPI_DB_VM_SSH_PRIVATE_KEY:-}" || -n "${QPI_DB_VM_SSH_KEY_PATH:-}" ]]; then
    qpi_prepare_private_key \
      "QPI_DB_VM_SSH_PRIVATE_KEY" \
      "QPI_DB_VM_SSH_KEY_PATH" \
      "${HOME}/.ssh/id_rsa" \
      "db_ssh_key_path" \
      "db_generated_ssh_key"
  else
    db_ssh_key_path="${bot_ssh_key_path}"
    db_generated_ssh_key="0"
  fi
}

fetch_remote_database_url() {
  ssh_bot "awk 'index(\$0, \"DATABASE_URL=\") == 1 { sub(/^DATABASE_URL=/, \"\"); print; exit }' /etc/qpi/bot.env"
}

parse_remote_database_url() {
  REMOTE_DATABASE_URL="$1" python3 - <<'PY'
from urllib.parse import urlparse
import os

parsed = urlparse(os.environ["REMOTE_DATABASE_URL"])
print(parsed.hostname or "127.0.0.1")
print(parsed.port or 5432)
print((parsed.path or "/qpi").lstrip("/") or "qpi")
print(parsed.username or "qpi")
PY
}

load_remote_db_context() {
  local remote_database_url

  remote_database_url="$(fetch_remote_database_url)"
  if [[ -z "${remote_database_url}" ]]; then
    echo "DATABASE_URL missing in /etc/qpi/bot.env on ${BOT_VM_HOST}" >&2
    exit 1
  fi

  mapfile -t remote_db_parts < <(parse_remote_database_url "${remote_database_url}")
  remote_db_host="${remote_db_parts[0]}"
  remote_db_port="${remote_db_parts[1]}"
  remote_db_name="${remote_db_parts[2]}"
  remote_db_user="${remote_db_parts[3]}"
  QPI_DB_VM_HOST="${QPI_DB_VM_HOST:-${remote_db_host}}"
}

generate_password() {
  openssl rand -hex 32
}

fetch_existing_mcp_password() {
  ssh_bot \
    "sudo awk -F= '\$1 == \"DBHUB_QPI_DB_PASSWORD\" { sub(/^[^=]*=/, \"\"); print; exit }' /etc/qpi/qpi-pg-mcp.env 2>/dev/null || true"
}

sql_ident() {
  SQL_IDENT_VALUE="$1" python3 - <<'PY'
import os

value = os.environ["SQL_IDENT_VALUE"]
print('"' + value.replace('"', '""') + '"')
PY
}

sql_literal() {
  SQL_LITERAL_VALUE="$1" python3 - <<'PY'
import os

value = os.environ["SQL_LITERAL_VALUE"]
print("'" + value.replace("'", "''") + "'")
PY
}

build_role_sql() {
  local role_ident
  local password_literal
  local db_ident

  role_ident="$(sql_ident "${QPI_MCP_DB_ROLE}")"
  password_literal="$(sql_literal "${mcp_password}")"
  db_ident="$(sql_ident "${remote_db_name}")"

  cat <<SQL
DO \$qpi_mcp\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$(printf '%s' "${QPI_MCP_DB_ROLE}" | sed "s/'/''/g")') THEN
    CREATE ROLE ${role_ident} LOGIN PASSWORD ${password_literal} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  ELSE
    ALTER ROLE ${role_ident} WITH LOGIN PASSWORD ${password_literal} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
\$qpi_mcp\$;

ALTER ROLE ${role_ident} SET default_transaction_read_only = 'on';
ALTER ROLE ${role_ident} SET statement_timeout = '10s';
ALTER ROLE ${role_ident} SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE ${role_ident} SET application_name = 'qpi-mcp';
GRANT CONNECT ON DATABASE ${db_ident} TO ${role_ident};
SQL
}

build_grant_sql() {
  local role_ident
  local app_user_ident

  role_ident="$(sql_ident "${QPI_MCP_DB_ROLE}")"
  app_user_ident="$(sql_ident "${remote_db_user}")"

  cat <<SQL
GRANT USAGE ON SCHEMA public TO ${role_ident};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${role_ident};
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ${role_ident};
ALTER DEFAULT PRIVILEGES FOR ROLE ${app_user_ident} IN SCHEMA public GRANT SELECT ON TABLES TO ${role_ident};
ALTER DEFAULT PRIVILEGES FOR ROLE ${app_user_ident} IN SCHEMA public GRANT SELECT ON SEQUENCES TO ${role_ident};
SQL
}

run_db_admin_sql() {
  local database_name="$1"
  ssh_db "sudo -u postgres psql -v ON_ERROR_STOP=1 -d $(shell_quote "${database_name}")"
}

install_db_role() {
  echo "Ensuring PostgreSQL read-only MCP role ${QPI_MCP_DB_ROLE}"
  build_role_sql | run_db_admin_sql "postgres"
  build_grant_sql | run_db_admin_sql "${remote_db_name}"
}

ensure_remote_docker() {
  # shellcheck disable=SC2016
  ssh_bot 'set -euo pipefail
if command -v docker >/dev/null 2>&1; then
  exit 0
fi

echo "Docker is missing on this bot VM; installing Docker CE to match current cloud-init." >&2
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg lsb-release
sudo install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
fi
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
printf "deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n" \
  "$(dpkg --print-architecture)" \
  "${VERSION_CODENAME}" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu || true
'
}

remote_install_stdin() {
  local owner="$1"
  local group="$2"
  local mode="$3"
  local path="$4"

  ssh_bot "tmp=\$(mktemp); cat > \"\$tmp\"; sudo install -o $(shell_quote "${owner}") -g $(shell_quote "${group}") -m $(shell_quote "${mode}") \"\$tmp\" $(shell_quote "${path}"); rm -f \"\$tmp\""
}

render_env_file() {
  cat <<EOF
DBHUB_QPI_DB_HOST=${remote_db_host}
DBHUB_QPI_DB_PORT=${remote_db_port}
DBHUB_QPI_DB_NAME=${remote_db_name}
DBHUB_QPI_DB_USER=${QPI_MCP_DB_ROLE}
DBHUB_QPI_DB_PASSWORD=${mcp_password}
DBHUB_LOG_LEVEL=warn
EOF
}

render_config_file() {
  cat <<EOF
[[sources]]
id = "qpi_prod"
description = "QPI production PostgreSQL. Use only for read-only diagnostics in /home/darker/dkarelov/qpi. Do not use the global pg-prod MCP for this repo."
type = "postgres"
host = "\${DBHUB_QPI_DB_HOST}"
port = ${remote_db_port}
database = "\${DBHUB_QPI_DB_NAME}"
user = "\${DBHUB_QPI_DB_USER}"
password = "\${DBHUB_QPI_DB_PASSWORD}"
sslmode = "disable"
search_path = "public"
connection_timeout = 5
query_timeout = 10
lazy = true

[[tools]]
name = "execute_sql"
source = "qpi_prod"
readonly = true
max_rows = 500

[[tools]]
name = "search_objects"
source = "qpi_prod"
EOF
}

render_wrapper() {
  cat <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

image_file="/etc/qpi/qpi-pg-mcp.image"
env_file="/etc/qpi/qpi-pg-mcp.env"
config_file="/etc/qpi/dbhub-qpi.toml"

if [[ ! -r "${image_file}" ]]; then
  echo "${image_file} is missing; run scripts/deploy/qpi_pg_mcp.sh install" >&2
  exit 1
fi
if [[ ! -r "${env_file}" ]]; then
  echo "${env_file} is missing; run scripts/deploy/qpi_pg_mcp.sh install" >&2
  exit 1
fi
if [[ ! -r "${config_file}" ]]; then
  echo "${config_file} is missing; run scripts/deploy/qpi_pg_mcp.sh install" >&2
  exit 1
fi

image="$(cat "${image_file}")"
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "DBHub image ${image} is not present locally; run scripts/deploy/qpi_pg_mcp.sh install" >&2
  exit 1
fi

docker run --rm -i --pull never \
  --network host \
  --env-file "${env_file}" \
  -v "${config_file}:/etc/dbhub/dbhub.toml:ro" \
  "${image}" \
  --transport stdio \
  --config /etc/dbhub/dbhub.toml \
  | while IFS= read -r line; do
      if [[ "${line}" == \{* ]]; then
        printf '%s\n' "${line}"
      else
        printf '%s\n' "${line}" >&2
      fi
    done
EOF
}

install_bot_files() {
  local quoted_image
  local resolved_image

  quoted_image="$(shell_quote "${DBHUB_IMAGE}")"
  ensure_remote_docker
  echo "Pulling DBHub image ${DBHUB_IMAGE} on ${BOT_VM_HOST}"
  ssh_bot "sudo docker pull --platform linux/amd64 ${quoted_image} >/dev/null"
  resolved_image="$(ssh_bot "sudo docker image inspect --format '{{index .RepoDigests 0}}' ${quoted_image}")"
  if [[ -z "${resolved_image}" ]]; then
    resolved_image="${DBHUB_IMAGE}"
  fi

  render_env_file | remote_install_stdin root ubuntu 0640 /etc/qpi/qpi-pg-mcp.env
  render_config_file | remote_install_stdin root root 0644 /etc/qpi/dbhub-qpi.toml
  printf '%s\n' "${resolved_image}" | remote_install_stdin root ubuntu 0644 /etc/qpi/qpi-pg-mcp.image
  render_wrapper | remote_install_stdin root root 0755 /usr/local/bin/qpi-pg-mcp
}

status() {
  # shellcheck disable=SC2016
  ssh_bot 'set -euo pipefail
sudo test -r /etc/qpi/qpi-pg-mcp.env
sudo test -r /etc/qpi/dbhub-qpi.toml
sudo test -r /etc/qpi/qpi-pg-mcp.image
sudo test -x /usr/local/bin/qpi-pg-mcp
image="$(sudo cat /etc/qpi/qpi-pg-mcp.image)"
sudo docker image inspect "${image}" >/dev/null
printf "qpi-pg-mcp installed: %s\n" "${image}"
'
}

smoke() {
  echo "Checking read-only DB credentials from ${BOT_VM_HOST}"
  ssh_bot 'sudo bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/qpi/current
set -a
. /etc/qpi/qpi-pg-mcp.env
set +a
.venv/bin/python - <<'PY'
import os
import uuid

import psycopg

conn = psycopg.connect(
    host=os.environ["DBHUB_QPI_DB_HOST"],
    port=int(os.environ["DBHUB_QPI_DB_PORT"]),
    dbname=os.environ["DBHUB_QPI_DB_NAME"],
    user=os.environ["DBHUB_QPI_DB_USER"],
    password=os.environ["DBHUB_QPI_DB_PASSWORD"],
    options="-c default_transaction_read_only=on -c statement_timeout=10000",
)
with conn:
    with conn.cursor() as cur:
        cur.execute("select current_user, current_setting('transaction_read_only')")
        user, read_only = cur.fetchone()
        cur.execute(
            """
            select count(*)
            from information_schema.tables
            where table_schema = 'public'
            """
        )
        table_count = cur.fetchone()[0]
        blocked_table = "qpi_mcp_should_fail_" + uuid.uuid4().hex
        try:
            cur.execute(f'create table "{blocked_table}" (id integer)')
        except Exception as exc:
            conn.rollback()
            print(f"user={user} transaction_read_only={read_only} public_tables={table_count} write_blocked={exc.__class__.__name__}")
        else:
            cur.execute(f'drop table if exists "{blocked_table}"')
            raise SystemExit("read-only smoke failed: CREATE TABLE unexpectedly succeeded")
PY
REMOTE
}

uninstall() {
  ssh_bot 'sudo rm -f /usr/local/bin/qpi-pg-mcp /etc/qpi/dbhub-qpi.toml /etc/qpi/qpi-pg-mcp.env /etc/qpi/qpi-pg-mcp.image'
}

qpi_require_command ssh
qpi_require_command ssh-keygen
qpi_require_command python3
qpi_require_command openssl
qpi_require_env BOT_VM_HOST

prepare_keys

case "${command_name}" in
  install)
    load_remote_db_context
    mcp_password="$(fetch_existing_mcp_password)"
    if [[ -z "${mcp_password}" ]]; then
      mcp_password="$(generate_password)"
    fi
    install_db_role
    install_bot_files
    status
    ;;
  status)
    status
    ;;
  smoke)
    smoke
    ;;
  rotate-secret)
    load_remote_db_context
    mcp_password="$(generate_password)"
    install_db_role
    install_bot_files
    status
    ;;
  uninstall)
    uninstall
    ;;
esac
