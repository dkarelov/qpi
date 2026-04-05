#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
artifacts_dir="${repo_root}/.artifacts/support-bot"

# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF' >&2
usage: support_bot.sh <image-ref>

Required environment:
  YC_FOLDER_ID
  SUPPORT_BOT_INSTANCE_GROUP_NAME
  SUPPORT_BOT_TELEGRAM_BOT_TOKEN
  SUPPORT_BOT_STAFFCHAT_ID
  SUPPORT_BOT_OWNER_ID

SSH auth:
  SUPPORT_BOT_VM_SSH_PRIVATE_KEY or SUPPORT_BOT_VM_SSH_KEY_PATH

Optional environment:
  SUPPORT_BOT_VM_SSH_USER (default: ubuntu)
  SUPPORT_BOT_VM_SSH_PORT (default: 22)
  SUPPORT_BOT_VM_SSH_PROXY_HOST
  SUPPORT_BOT_VM_SSH_PROXY_USER (default: ubuntu)
  SUPPORT_BOT_VM_SSH_PROXY_PORT (default: 22)
  YC_TOKEN
  QPI_PREDEPLOY_ONLY (default: 0)
  SUPPORT_BOT_RELEASE_ID
  SUPPORT_BOT_ARTIFACT_RETENTION_COUNT (default: 10)
  SUPPORT_BOT_ARTIFACT_RETENTION_DAYS (default: 14)
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

image_ref="$1"
QPI_PREDEPLOY_ONLY="${QPI_PREDEPLOY_ONLY:-0}"

SUPPORT_BOT_VM_SSH_USER="${SUPPORT_BOT_VM_SSH_USER:-ubuntu}"
SUPPORT_BOT_VM_SSH_PORT="${SUPPORT_BOT_VM_SSH_PORT:-22}"
SUPPORT_BOT_VM_SSH_PROXY_USER="${SUPPORT_BOT_VM_SSH_PROXY_USER:-ubuntu}"
SUPPORT_BOT_VM_SSH_PROXY_PORT="${SUPPORT_BOT_VM_SSH_PROXY_PORT:-22}"
SUPPORT_BOT_ARTIFACT_RETENTION_COUNT="${SUPPORT_BOT_ARTIFACT_RETENTION_COUNT:-10}"
SUPPORT_BOT_ARTIFACT_RETENTION_DAYS="${SUPPORT_BOT_ARTIFACT_RETENTION_DAYS:-14}"

generated_ssh_key=0
ssh_key_path=""
temp_dir=""
support_bot_host=""

cleanup() {
  if [[ -n "${temp_dir}" && -d "${temp_dir}" ]]; then
    rm -rf "${temp_dir}"
  fi
  if [[ "${generated_ssh_key}" == "1" && -n "${ssh_key_path}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

resolve_support_bot_host() {
  support_bot_host="$(qpi_resolve_support_bot_host "${YC_FOLDER_ID}" "${SUPPORT_BOT_INSTANCE_GROUP_NAME}")"
  if [[ -z "${support_bot_host}" ]]; then
    echo "Failed to resolve a private IP for ${SUPPORT_BOT_INSTANCE_GROUP_NAME}." >&2
    exit 1
  fi
}

remote_exec() {
  # shellcheck disable=SC2029
  ssh \
    "${ssh_args[@]}" \
    "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}" \
    "$@"
}

remote_output() {
  remote_exec "$@"
}

prune_artifacts() {
  qpi_require_nonnegative_integer "SUPPORT_BOT_ARTIFACT_RETENTION_COUNT" "${SUPPORT_BOT_ARTIFACT_RETENTION_COUNT}"
  qpi_require_nonnegative_integer "SUPPORT_BOT_ARTIFACT_RETENTION_DAYS" "${SUPPORT_BOT_ARTIFACT_RETENTION_DAYS}"

  mkdir -p "${artifacts_dir}"

  find "${artifacts_dir}" -maxdepth 1 -type f -name 'support-bot-release-*.tar.gz' \
    -mtime +"${SUPPORT_BOT_ARTIFACT_RETENTION_DAYS}" -delete

  mapfile -t archives < <(
    find "${artifacts_dir}" -maxdepth 1 -type f -name 'support-bot-release-*.tar.gz' -printf '%T@ %p\n' |
      sort -nr |
      awk '{sub(/^[^ ]+ /, ""); print}'
  )

  if (( ${#archives[@]} > SUPPORT_BOT_ARTIFACT_RETENTION_COUNT )); then
    for archive_path in "${archives[@]:SUPPORT_BOT_ARTIFACT_RETENTION_COUNT}"; do
      rm -f "${archive_path}"
    done
  fi
}

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

run_preflight() {
  eval "$("${script_dir}/preflight.sh" support-bot)"
}

qpi_timing_init

qpi_phase_start "preflight"
run_preflight
qpi_phase_end

qpi_configure_yc_cli
resolve_support_bot_host
qpi_prepare_private_key "SUPPORT_BOT_VM_SSH_PRIVATE_KEY" "SUPPORT_BOT_VM_SSH_KEY_PATH" "${HOME}/.ssh/id_rsa" ssh_key_path generated_ssh_key

ssh_args=(
  -p "${SUPPORT_BOT_VM_SSH_PORT}"
  -i "${ssh_key_path}"
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
)
scp_args=(
  -P "${SUPPORT_BOT_VM_SSH_PORT}"
  -i "${ssh_key_path}"
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
)

if [[ -n "${SUPPORT_BOT_VM_SSH_PROXY_HOST:-}" ]]; then
  proxy_command="ssh -p ${SUPPORT_BOT_VM_SSH_PROXY_PORT} -i ${ssh_key_path} -o StrictHostKeyChecking=accept-new -W %h:%p ${SUPPORT_BOT_VM_SSH_PROXY_USER}@${SUPPORT_BOT_VM_SSH_PROXY_HOST}"
  ssh_args+=(-o "ProxyCommand=${proxy_command}")
  scp_args+=(-o "ProxyCommand=${proxy_command}")
fi

qpi_phase_start "package"
mkdir -p "${artifacts_dir}"
prune_artifacts

release_stamp="$(date -u +%Y%m%d%H%M%S)"
release_sha="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
release_id="${SUPPORT_BOT_RELEASE_ID:-${release_stamp}-${release_sha}}"

temp_dir="$(mktemp -d)"
release_stage="${temp_dir}/release"
release_archive="${artifacts_dir}/support-bot-release-${release_id}.tar.gz"
rendered_config="${temp_dir}/config.yaml"

mkdir -p "${release_stage}"
cp "${repo_root}/apps/support-bot/compose.prod.yml" "${release_stage}/compose.prod.yml"
cat > "${release_stage}/.env" <<EOF
SUPPORT_BOT_IMAGE=${image_ref}
EOF

tar -czf "${release_archive}" -C "${release_stage}" .

sed \
  -e "s/__SUPPORT_BOT_TELEGRAM_BOT_TOKEN__/$(escape_sed_replacement "${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}")/g" \
  -e "s/__SUPPORT_BOT_STAFFCHAT_ID__/$(escape_sed_replacement "${SUPPORT_BOT_STAFFCHAT_ID}")/g" \
  -e "s/__SUPPORT_BOT_OWNER_ID__/$(escape_sed_replacement "${SUPPORT_BOT_OWNER_ID}")/g" \
  "${repo_root}/apps/support-bot/config/config.template.yaml" > "${rendered_config}"
qpi_phase_end

if [[ "${QPI_PREDEPLOY_ONLY}" == "1" ]]; then
  echo "release_id=${release_id}"
  echo "support_bot_host=${support_bot_host}"
  echo "image_ref=${image_ref}"
  echo "release_archive=${release_archive}"
  qpi_emit_timing_summary "Support Bot Deploy"
  exit 0
fi

qpi_phase_start "upload"
scp "${scp_args[@]}" \
  "${release_archive}" \
  "${repo_root}/infra/scripts/remote_rollout_support_bot.sh" \
  "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}:/tmp/"

scp "${scp_args[@]}" \
  "${rendered_config}" \
  "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}:/tmp/support-bot-config.yaml"
qpi_phase_end

qpi_phase_start "rollout"
remote_exec \
  "set -euo pipefail && \
   sudo install -d -m 0755 /etc/support-bot /opt/support-bot/releases /var/lib/support-bot /var/lib/support-bot/mongodb && \
   if sudo test -f /etc/support-bot/config.yaml; then \
     sudo cp /etc/support-bot/config.yaml /tmp/support-bot-config.previous.yaml; \
   fi && \
   sudo install -m 0640 /tmp/support-bot-config.yaml /etc/support-bot/config.yaml && \
   sudo chown root:${SUPPORT_BOT_VM_SSH_USER} /etc/support-bot/config.yaml && \
   chmod +x /tmp/remote_rollout_support_bot.sh && \
   if ! /tmp/remote_rollout_support_bot.sh '${release_id}' '/tmp/$(basename "${release_archive}")' '${image_ref}'; then \
     if sudo test -f /tmp/support-bot-config.previous.yaml; then \
       sudo install -m 0640 /tmp/support-bot-config.previous.yaml /etc/support-bot/config.yaml; \
       sudo chown root:${SUPPORT_BOT_VM_SSH_USER} /etc/support-bot/config.yaml; \
     fi; \
     exit 1; \
   fi"
qpi_phase_end

qpi_phase_start "smoke"
mongo_ping="$(
  remote_output \
    "sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml \
      exec -T mongodb mongosh --quiet --eval 'db.adminCommand({ ping: 1 }).ok' mongodb://127.0.0.1:27017/admin"
)"

telegram_get_me="$(curl -fsSL "https://api.telegram.org/bot${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}/getMe")"
if ! jq -e '.ok == true' >/dev/null <<<"${telegram_get_me}"; then
  echo "Telegram getMe failed." >&2
  exit 1
fi
qpi_phase_end

echo "release_id=${release_id}"
echo "support_bot_host=${support_bot_host}"
echo "image_ref=${image_ref}"
echo "mongo_ping=${mongo_ping}"
echo "telegram_get_me_ok=true"

qpi_append_step_summary "### Support Bot Deploy Result"
qpi_append_step_summary ""
qpi_append_step_summary "- Release ID: \`${release_id}\`"
qpi_append_step_summary "- Image: \`${image_ref}\`"
qpi_append_step_summary "- Mongo ping: \`${mongo_ping}\`"
qpi_append_step_summary ""
qpi_emit_timing_summary "Support Bot Deploy"
