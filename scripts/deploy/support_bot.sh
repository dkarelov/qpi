#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
artifacts_dir="${repo_root}/.artifacts/support-bot"

usage() {
  cat <<'EOF' >&2
usage: support_bot.sh <image-archive> <image-tag>

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
  SUPPORT_BOT_ARTIFACT_RETENTION_COUNT (default: 10)
  SUPPORT_BOT_ARTIFACT_RETENTION_DAYS (default: 14)
EOF
}

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

image_archive="$1"
image_tag="$2"

if [[ ! -f "${image_archive}" ]]; then
  echo "Image archive not found: ${image_archive}" >&2
  exit 1
fi

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

require_nonnegative_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${name} must be a non-negative integer." >&2
    exit 1
  fi
}

configure_yc_cli() {
  if [[ -n "${YC_TOKEN:-}" ]]; then
    yc config set token "${YC_TOKEN}" >/dev/null
  fi
  yc config set folder-id "${YC_FOLDER_ID}" >/dev/null
}

prepare_ssh_key() {
  local key_source
  if [[ -n "${SUPPORT_BOT_VM_SSH_PRIVATE_KEY:-}" ]]; then
    ssh_key_path="$(mktemp)"
    chmod 600 "${ssh_key_path}"
    printf '%s' "${SUPPORT_BOT_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      printf '%b' "${SUPPORT_BOT_VM_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    fi
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      if ! printf '%s' "${SUPPORT_BOT_VM_SSH_PRIVATE_KEY}" | base64 -d > "${ssh_key_path}" 2>/dev/null; then
        :
      fi
    fi
    sed -i 's/\r$//' "${ssh_key_path}"
    generated_ssh_key=1
  else
    key_source="${SUPPORT_BOT_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    ssh_key_path="${key_source}"
    generated_ssh_key=0
  fi

  if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
    echo "Failed to decode SUPPORT_BOT_VM_SSH_PRIVATE_KEY into a valid private key." >&2
    exit 1
  fi
}

cleanup() {
  if [[ -n "${temp_dir:-}" && -d "${temp_dir}" ]]; then
    rm -rf "${temp_dir}"
  fi
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

resolve_support_bot_host() {
  local payload
  payload="$(
    yc compute instance-group list-instances \
      --folder-id "${YC_FOLDER_ID}" \
      --name "${SUPPORT_BOT_INSTANCE_GROUP_NAME}" \
      --format json
  )"

  jq -r '
    (if type == "array" then . else (.instances // .items // []) end) as $items
    | if ($items | length) == 0 then empty else $items[0] end
    | .network_interfaces[0].primary_v4_address.address
      // .networkInterfaces[0].primaryV4Address.address
      // empty
  ' <<<"${payload}"
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
  require_nonnegative_integer "SUPPORT_BOT_ARTIFACT_RETENTION_COUNT" "${SUPPORT_BOT_ARTIFACT_RETENTION_COUNT}"
  require_nonnegative_integer "SUPPORT_BOT_ARTIFACT_RETENTION_DAYS" "${SUPPORT_BOT_ARTIFACT_RETENTION_DAYS}"

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

require_command yc
require_command jq
require_command scp
require_command ssh

require_env "YC_FOLDER_ID"
require_env "SUPPORT_BOT_INSTANCE_GROUP_NAME"
require_env "SUPPORT_BOT_TELEGRAM_BOT_TOKEN"
require_env "SUPPORT_BOT_STAFFCHAT_ID"
require_env "SUPPORT_BOT_OWNER_ID"

SUPPORT_BOT_VM_SSH_USER="${SUPPORT_BOT_VM_SSH_USER:-ubuntu}"
SUPPORT_BOT_VM_SSH_PORT="${SUPPORT_BOT_VM_SSH_PORT:-22}"
SUPPORT_BOT_VM_SSH_PROXY_USER="${SUPPORT_BOT_VM_SSH_PROXY_USER:-ubuntu}"
SUPPORT_BOT_VM_SSH_PROXY_PORT="${SUPPORT_BOT_VM_SSH_PROXY_PORT:-22}"
SUPPORT_BOT_ARTIFACT_RETENTION_COUNT="${SUPPORT_BOT_ARTIFACT_RETENTION_COUNT:-10}"
SUPPORT_BOT_ARTIFACT_RETENTION_DAYS="${SUPPORT_BOT_ARTIFACT_RETENTION_DAYS:-14}"

configure_yc_cli
prepare_ssh_key

support_bot_host="$(resolve_support_bot_host)"
if [[ -z "${support_bot_host}" ]]; then
  echo "Failed to resolve a private IP for ${SUPPORT_BOT_INSTANCE_GROUP_NAME}." >&2
  exit 1
fi

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
SUPPORT_BOT_IMAGE=${image_tag}
EOF

tar -czf "${release_archive}" -C "${release_stage}" .

sed \
  -e "s/__SUPPORT_BOT_TELEGRAM_BOT_TOKEN__/$(escape_sed_replacement "${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}")/g" \
  -e "s/__SUPPORT_BOT_STAFFCHAT_ID__/$(escape_sed_replacement "${SUPPORT_BOT_STAFFCHAT_ID}")/g" \
  -e "s/__SUPPORT_BOT_OWNER_ID__/$(escape_sed_replacement "${SUPPORT_BOT_OWNER_ID}")/g" \
  "${repo_root}/apps/support-bot/config/config.template.yaml" > "${rendered_config}"

scp "${scp_args[@]}" \
  "${release_archive}" \
  "${image_archive}" \
  "${repo_root}/infra/scripts/remote_rollout_support_bot.sh" \
  "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}:/tmp/"

scp "${scp_args[@]}" \
  "${rendered_config}" \
  "${SUPPORT_BOT_VM_SSH_USER}@${support_bot_host}:/tmp/support-bot-config.yaml"

remote_exec \
  "set -euo pipefail && \
   sudo install -d -m 0755 /etc/support-bot /opt/support-bot/releases /var/lib/support-bot /var/lib/support-bot/mongodb && \
   if sudo test -f /etc/support-bot/config.yaml; then \
     sudo cp /etc/support-bot/config.yaml /tmp/support-bot-config.previous.yaml; \
   fi && \
   sudo install -m 0640 /tmp/support-bot-config.yaml /etc/support-bot/config.yaml && \
   sudo chown root:${SUPPORT_BOT_VM_SSH_USER} /etc/support-bot/config.yaml && \
   chmod +x /tmp/remote_rollout_support_bot.sh && \
   if ! /tmp/remote_rollout_support_bot.sh '${release_id}' '/tmp/$(basename "${release_archive}")' '/tmp/$(basename "${image_archive}")'; then \
     if sudo test -f /tmp/support-bot-config.previous.yaml; then \
       sudo install -m 0640 /tmp/support-bot-config.previous.yaml /etc/support-bot/config.yaml; \
       sudo chown root:${SUPPORT_BOT_VM_SSH_USER} /etc/support-bot/config.yaml; \
     fi; \
     exit 1; \
   fi"

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

cat <<EOF
release_id=${release_id}
support_bot_host=${support_bot_host}
image_tag=${image_tag}
mongo_ping=${mongo_ping}
telegram_get_me_ok=true
EOF
