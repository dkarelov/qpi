#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

usage() {
  cat <<'EOF' >&2
usage:
  qpi_pg_mcp_codex.sh install
  qpi_pg_mcp_codex.sh doctor
  qpi_pg_mcp_codex.sh remove
  qpi_pg_mcp_codex.sh print-command

Registers the qpi-pg-prod MCP server in the local Codex config. The server is
launched through SSH stdio on the bot VM jump host.

Optional environment:
  BOT_VM_HOST (default: terraform -chdir=infra output -raw bot_public_ip)
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  CODEX_HOME (default: ~/.codex)
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

command_name="$1"
case "${command_name}" in
  install|doctor|remove|print-command)
    ;;
  *)
    usage
    exit 1
    ;;
esac

BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
CODEX_CONFIG_PATH="${CODEX_CONFIG_PATH:-${CODEX_HOME}/config.toml}"

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

resolve_bot_host() {
  if [[ -n "${BOT_VM_HOST:-}" ]]; then
    printf '%s\n' "${BOT_VM_HOST}"
    return
  fi

  if command -v terraform >/dev/null 2>&1 && [[ -d "${repo_root}/infra" ]]; then
    terraform -chdir="${repo_root}/infra" output -raw bot_public_ip 2>/dev/null || true
    return
  fi
}

ssh_command_args() {
  printf '%s\n' \
    ssh \
    -T \
    -i "${BOT_VM_SSH_KEY_PATH}" \
    -p "${BOT_VM_SSH_PORT}" \
    -o LogLevel=ERROR \
    -o StrictHostKeyChecking=accept-new \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    /usr/local/bin/qpi-pg-mcp
}

print_command() {
  local args=()
  mapfile -t args < <(ssh_command_args)
  printf 'codex mcp add qpi-pg-prod --'
  printf ' %q' "${args[@]}"
  printf '\n'
}

ensure_approval_mode() {
  mkdir -p "$(dirname "${CODEX_CONFIG_PATH}")"
  touch "${CODEX_CONFIG_PATH}"

  if ! grep -Fq '[mcp_servers.qpi-pg-prod.tools.execute_sql]' "${CODEX_CONFIG_PATH}"; then
    cat >> "${CODEX_CONFIG_PATH}" <<'EOF'

[mcp_servers.qpi-pg-prod.tools.execute_sql]
approval_mode = "approve"
EOF
  fi

  if ! grep -Fq '[mcp_servers.qpi-pg-prod.tools.execute_sql_qpi_prod]' "${CODEX_CONFIG_PATH}"; then
    cat >> "${CODEX_CONFIG_PATH}" <<'EOF'

[mcp_servers.qpi-pg-prod.tools.execute_sql_qpi_prod]
approval_mode = "approve"
EOF
  fi
}

install_server() {
  local args=()
  mapfile -t args < <(ssh_command_args)

  codex mcp remove qpi-pg-prod >/dev/null 2>&1 || true
  codex mcp add qpi-pg-prod -- "${args[@]}"
  ensure_approval_mode
  echo "Registered qpi-pg-prod in ${CODEX_CONFIG_PATH}"
}

doctor() {
  ssh \
    -T \
    -i "${BOT_VM_SSH_KEY_PATH}" \
    -p "${BOT_VM_SSH_PORT}" \
    -o LogLevel=ERROR \
    -o StrictHostKeyChecking=accept-new \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    'sudo test -x /usr/local/bin/qpi-pg-mcp && sudo test -r /etc/qpi/qpi-pg-mcp.env && sudo test -r /etc/qpi/dbhub-qpi.toml && sudo test -r /etc/qpi/qpi-pg-mcp.image'

  codex mcp list | grep -F 'qpi-pg-prod' >/dev/null
  codex doctor --summary --no-color --ascii
}

remove_server() {
  codex mcp remove qpi-pg-prod
}

require_command ssh
require_command codex

if [[ ! -f "${BOT_VM_SSH_KEY_PATH}" ]]; then
  echo "SSH key not found: ${BOT_VM_SSH_KEY_PATH}" >&2
  exit 1
fi

BOT_VM_HOST="$(resolve_bot_host)"
if [[ -z "${BOT_VM_HOST}" ]]; then
  echo "BOT_VM_HOST is required, or Terraform output bot_public_ip must be available." >&2
  exit 1
fi

case "${command_name}" in
  install)
    install_server
    ;;
  doctor)
    doctor
    ;;
  remove)
    remove_server
    ;;
  print-command)
    print_command
    ;;
esac
