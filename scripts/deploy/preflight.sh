#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF' >&2
usage:
  preflight.sh runtime
  preflight.sh functions [--skip-schema-check]
  preflight.sh support-bot
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

mode="$1"
shift
skip_schema_check="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-schema-check)
      skip_schema_check="1"
      shift
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

BOT_VM_INSTANCE_GROUP_NAME="${BOT_VM_INSTANCE_GROUP_NAME:-qpi-bot-ig}"
BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
SUPPORT_BOT_VM_SSH_USER="${SUPPORT_BOT_VM_SSH_USER:-ubuntu}"
SUPPORT_BOT_VM_SSH_PORT="${SUPPORT_BOT_VM_SSH_PORT:-22}"
QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE="${QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE:-0}"
TELEGRAM_UPDATE_MODE="${TELEGRAM_UPDATE_MODE:-polling}"

generated_ssh_key=0
ssh_key_path=""

cleanup() {
  if [[ "${generated_ssh_key}" == "1" && -n "${ssh_key_path}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

runtime_remote_exec() {
  ssh \
    -p "${BOT_VM_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    "$@"
}

support_bot_remote_exec() {
  # shellcheck disable=SC2029
  ssh_args=(
    -p "${SUPPORT_BOT_VM_SSH_PORT}"
    -i "${ssh_key_path}"
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=3
  )

  if [[ -n "${SUPPORT_BOT_VM_SSH_PROXY_HOST:-}" ]]; then
    proxy_user="${SUPPORT_BOT_VM_SSH_PROXY_USER:-ubuntu}"
    proxy_port="${SUPPORT_BOT_VM_SSH_PROXY_PORT:-22}"
    proxy_command="ssh -p ${proxy_port} -i ${ssh_key_path} -o StrictHostKeyChecking=accept-new -W %h:%p ${proxy_user}@${SUPPORT_BOT_VM_SSH_PROXY_HOST}"
    ssh_args+=(-o "ProxyCommand=${proxy_command}")
  fi

  # shellcheck disable=SC2029
  ssh "${ssh_args[@]}" "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}" "$@"
}

runtime_telegram_get_me() {
  local token_quoted
  local proxy_urls_quoted

  qpi_reject_legacy_telegram_api_proxy_url
  qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 2

  printf -v token_quoted "%q" "${TELEGRAM_BOT_TOKEN}"
  printf -v proxy_urls_quoted "%q" "${TELEGRAM_API_PROXY_URLS:-}"

  if runtime_remote_exec "bash -s" <<REMOTE
set -euo pipefail
TELEGRAM_BOT_TOKEN=${token_quoted}
TELEGRAM_API_PROXY_URLS=${proxy_urls_quoted}
mapfile -t telegram_proxy_urls < <(python3 - "\${TELEGRAM_API_PROXY_URLS}" <<'PY'
import re
import sys

for raw_item in re.split(r"[,\\n]+", sys.argv[1]):
    item = raw_item.strip()
    if item:
        print(item)
PY
)
telegram_get_me=""
telegram_username=""
for _round in 1 2 3; do
  for proxy_url in "\${telegram_proxy_urls[@]}"; do
    curl_args=(-fsS --connect-timeout 5 --max-time 15 --proxy "\${proxy_url}")
    if telegram_get_me="\$(curl "\${curl_args[@]}" "https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN}/getMe")" &&
      jq -e '.ok == true' >/dev/null <<<"\${telegram_get_me}"; then
      telegram_username="\$(jq -r '.result.username // "-"' <<<"\${telegram_get_me}")"
      printf 'telegram_get_me_ok=%q\n' "true"
      printf 'telegram_get_me_username=%q\n' "\${telegram_username}"
      exit 0
    fi
  done
done
exit 1
REMOTE
  then
    return 0
  fi

  if [[ "${QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE}" == "1" ]]; then
    echo "Telegram getMe failed, continuing because QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1." >&2
    printf 'telegram_get_me_ok=%q\n' "false"
    printf 'telegram_get_me_username=%q\n' "unknown"
    return 0
  fi

  echo "Telegram getMe failed. Set QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1 only for intentional emergency deploys." >&2
  return 1
}

qpi_timing_init
qpi_phase_start "validate"

case "${mode}" in
  runtime)
    qpi_require_command yc
    qpi_require_command ssh
    qpi_require_command ssh-keygen
    qpi_require_command python3
    qpi_require_env YC_FOLDER_ID
    qpi_require_env BOT_VM_HOST
    qpi_require_env TELEGRAM_BOT_TOKEN
    qpi_require_env TOKEN_CIPHER_KEY
    qpi_require_env TELEGRAM_API_PROXY_URLS
    qpi_validate_telegram_update_mode "${TELEGRAM_UPDATE_MODE}"
    if [[ "${TELEGRAM_UPDATE_MODE}" == "webhook" ]]; then
      qpi_require_env BOT_WEBHOOK_SECRET_TOKEN
    fi
    qpi_reject_legacy_telegram_api_proxy_url
    qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 2
    qpi_prepare_private_key "BOT_VM_SSH_PRIVATE_KEY" "BOT_VM_SSH_KEY_PATH" "${HOME}/.ssh/id_rsa" ssh_key_path generated_ssh_key
    qpi_configure_yc_cli

    if ! qpi_verify_host_in_instance_group "${YC_FOLDER_ID}" "${BOT_VM_INSTANCE_GROUP_NAME}" "${BOT_VM_HOST}"; then
      echo "BOT_VM_HOST=${BOT_VM_HOST} is not part of instance group ${BOT_VM_INSTANCE_GROUP_NAME} in folder ${YC_FOLDER_ID}." >&2
      exit 1
    fi

    runtime_schema_action="$(qpi_detect_runtime_schema_action)"
    ;;
  functions)
    qpi_require_command yc
    qpi_require_command ssh
    qpi_require_command ssh-keygen
    qpi_require_command python3
    qpi_require_env GH_TOKEN
    qpi_require_env YC_FOLDER_ID
    qpi_require_env BOT_VM_HOST
    qpi_prepare_private_key "BOT_VM_SSH_PRIVATE_KEY" "BOT_VM_SSH_KEY_PATH" "${HOME}/.ssh/id_rsa" ssh_key_path generated_ssh_key
    qpi_configure_yc_cli

    if ! qpi_verify_host_in_instance_group "${YC_FOLDER_ID}" "${BOT_VM_INSTANCE_GROUP_NAME}" "${BOT_VM_HOST}"; then
      echo "BOT_VM_HOST=${BOT_VM_HOST} is not part of instance group ${BOT_VM_INSTANCE_GROUP_NAME} in folder ${YC_FOLDER_ID}." >&2
      exit 1
    fi
    ;;
  support-bot)
    qpi_require_command yc
    qpi_require_command ssh
    qpi_require_command ssh-keygen
    qpi_require_command python3
    qpi_require_env YC_FOLDER_ID
    qpi_require_env SUPPORT_BOT_INSTANCE_GROUP_NAME
    qpi_require_env SUPPORT_BOT_TELEGRAM_BOT_TOKEN
    qpi_require_env SUPPORT_BOT_STAFFCHAT_ID
    qpi_require_env SUPPORT_BOT_OWNER_ID
    qpi_prepare_private_key "SUPPORT_BOT_VM_SSH_PRIVATE_KEY" "SUPPORT_BOT_VM_SSH_KEY_PATH" "${HOME}/.ssh/id_rsa" ssh_key_path generated_ssh_key
    qpi_configure_yc_cli
    support_bot_host="$(qpi_resolve_support_bot_host "${YC_FOLDER_ID}" "${SUPPORT_BOT_INSTANCE_GROUP_NAME}")"
    if [[ -z "${support_bot_host}" ]]; then
      echo "Failed to resolve a private IP for ${SUPPORT_BOT_INSTANCE_GROUP_NAME}." >&2
      exit 1
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
qpi_phase_end

qpi_phase_start "connectivity"
case "${mode}" in
  runtime|functions)
    runtime_remote_exec "true" >/dev/null
    ;;
  support-bot)
    support_bot_remote_exec "true" >/dev/null
    ;;
esac
qpi_phase_end

if [[ "${mode}" == "runtime" ]]; then
  qpi_phase_start "telegram"
  runtime_telegram_get_me
  qpi_phase_end
fi

if [[ "${mode}" == "runtime" ]]; then
  qpi_phase_start "schema"
  if [[ "${runtime_schema_action}" == "assert-clean" ]]; then
    BOT_VM_HOST="${BOT_VM_HOST}" \
    BOT_VM_SSH_USER="${BOT_VM_SSH_USER}" \
    BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT}" \
    BOT_VM_SSH_PRIVATE_KEY="${BOT_VM_SSH_PRIVATE_KEY:-}" \
    BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-}" \
    "${repo_root}/scripts/deploy/schema_remote.sh" assert-clean >/dev/null
  fi
  qpi_phase_end
  printf 'runtime_schema_action=%q\n' "${runtime_schema_action}"
fi

if [[ "${mode}" == "functions" && "${skip_schema_check}" != "1" ]]; then
  qpi_phase_start "schema"
  BOT_VM_HOST="${BOT_VM_HOST}" \
  BOT_VM_SSH_USER="${BOT_VM_SSH_USER}" \
  BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT}" \
  BOT_VM_SSH_PRIVATE_KEY="${BOT_VM_SSH_PRIVATE_KEY:-}" \
  BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-}" \
  "${repo_root}/scripts/deploy/schema_remote.sh" assert-clean >/dev/null
  qpi_phase_end
fi

printf 'preflight_mode=%q\n' "${mode}"
printf 'preflight_skip_schema_check=%q\n' "${skip_schema_check}"
qpi_emit_timing_summary "Deploy Preflight (${mode})"
