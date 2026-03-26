#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
artifacts_dir="${repo_root}/.artifacts/runtime"
schema_mode="${QPI_DEPLOY_SCHEMA_MODE:-auto}"

usage() {
  cat <<'EOF' >&2
usage: runtime.sh

Required environment:
  BOT_VM_HOST
  TELEGRAM_BOT_TOKEN
  TOKEN_CIPHER_KEY
  BOT_WEBHOOK_SECRET_TOKEN
  YC_FOLDER_ID

Optional environment:
  BOT_VM_INSTANCE_GROUP_NAME (default: qpi-bot-ig)
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_HEALTH_PORT (default: 18080)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  ADMIN_TELEGRAM_IDS
  SUPPORT_BOT_USERNAME
  GH_TOKEN or TOKEN_YC_JSON_LOGGER
  DEPLOY_BASE_SHA / DEPLOY_HEAD_SHA (for schema auto-detection)
  QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY (default: 0)
  QPI_DEPLOY_MIN_FREE_MB (default: 2048)
  QPI_RUNTIME_ARTIFACT_RETENTION_COUNT (default: 10)
  QPI_RUNTIME_ARTIFACT_RETENTION_DAYS (default: 14)
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
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

configure_yc_cli() {
  if [[ -n "${YC_TOKEN:-}" ]]; then
    yc config set token "${YC_TOKEN}" >/dev/null
  fi
  if [[ -n "${YC_FOLDER_ID:-}" ]]; then
    yc config set folder-id "${YC_FOLDER_ID}" >/dev/null
  fi
}

require_env "BOT_VM_HOST"
require_env "TELEGRAM_BOT_TOKEN"
require_env "TOKEN_CIPHER_KEY"
require_env "BOT_WEBHOOK_SECRET_TOKEN"
require_env "YC_FOLDER_ID"

BOT_VM_INSTANCE_GROUP_NAME="${BOT_VM_INSTANCE_GROUP_NAME:-qpi-bot-ig}"
BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
BOT_HEALTH_PORT="${BOT_HEALTH_PORT:-18080}"
QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY="${QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY:-0}"
QPI_DEPLOY_MIN_FREE_MB="${QPI_DEPLOY_MIN_FREE_MB:-2048}"
QPI_RUNTIME_ARTIFACT_RETENTION_COUNT="${QPI_RUNTIME_ARTIFACT_RETENTION_COUNT:-10}"
QPI_RUNTIME_ARTIFACT_RETENTION_DAYS="${QPI_RUNTIME_ARTIFACT_RETENTION_DAYS:-14}"

require_nonnegative_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${name} must be a non-negative integer." >&2
    exit 1
  fi
}

resolve_git_token() {
  if [[ -n "${GH_TOKEN:-}" ]]; then
    return
  fi
  if [[ -n "${TOKEN_YC_JSON_LOGGER:-}" ]]; then
    GH_TOKEN="${TOKEN_YC_JSON_LOGGER}"
    export GH_TOKEN
    return
  fi
  if command -v gh >/dev/null 2>&1; then
    GH_TOKEN="$(gh auth token 2>/dev/null || true)"
    export GH_TOKEN
  fi
}

detect_schema_apply() {
  case "${schema_mode}" in
    always)
      return 0
      ;;
    never)
      return 1
      ;;
    auto)
      ;;
    *)
      echo "Unsupported QPI_DEPLOY_SCHEMA_MODE: ${schema_mode}" >&2
      exit 1
      ;;
  esac

  local base_ref
  local head_ref
  local diff_target

  if [[ -n "${DEPLOY_BASE_SHA:-}" && -n "${DEPLOY_HEAD_SHA:-}" ]]; then
    base_ref="${DEPLOY_BASE_SHA}"
    head_ref="${DEPLOY_HEAD_SHA}"
    if git cat-file -e "${base_ref}^{commit}" 2>/dev/null && git cat-file -e "${head_ref}^{commit}" 2>/dev/null; then
      diff_target="$(git diff --name-only "${base_ref}" "${head_ref}")"
    else
      return 0
    fi
  elif [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
    return 0
  elif git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    diff_target="$(git diff --name-only HEAD^ HEAD)"
  else
    return 0
  fi

  if printf '%s\n' "${diff_target}" | grep -Eq '^(schema/|libs/db/|infra/scripts/remote_apply_schema\.sh$)'; then
    return 0
  fi
  return 1
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
  if [[ -n "${temp_dir:-}" && -d "${temp_dir}" ]]; then
    rm -rf "${temp_dir}"
  fi
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

yc_bot_instance_group_json() {
  yc compute instance-group list-instances \
    --folder-id "${YC_FOLDER_ID}" \
    --name "${BOT_VM_INSTANCE_GROUP_NAME}" \
    --format json
}

verify_target_vm() {
  require_command yc
  local resolved
  resolved="$(
    yc_bot_instance_group_json | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
expected = sys.argv[1]

if isinstance(payload, list):
    items = payload
else:
    items = payload.get("instances") or payload.get("items") or []
    if not isinstance(items, list):
        items = []

for item in items:
    addresses = []
    for interface in item.get("network_interfaces", []) or item.get("networkInterfaces", []) or []:
        primary = interface.get("primary_v4_address") or interface.get("primaryV4Address") or {}
        addresses.append(primary.get("address"))
        nat = primary.get("one_to_one_nat") or primary.get("oneToOneNat") or {}
        addresses.append(nat.get("address"))
    addresses = [address for address in addresses if address]
    if expected in addresses:
        print(item.get("id", "ok"))
        break
' "${BOT_VM_HOST}"
  )"

  if [[ -z "${resolved}" ]]; then
    echo "BOT_VM_HOST=${BOT_VM_HOST} is not part of instance group ${BOT_VM_INSTANCE_GROUP_NAME} in folder ${YC_FOLDER_ID}." >&2
    exit 1
  fi
}

remote_exec() {
  ssh \
    -p "${BOT_VM_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    "$@"
}

remote_output() {
  remote_exec "$@"
}

remote_preflight() {
  before_release_target="$(remote_output "readlink -f /opt/qpi/current || true")"
  before_service_state="$(remote_output "systemctl is-active qpi-bot.service || true")"
  free_mb="$(remote_output "df -Pm /opt/qpi/releases | awk 'NR==2 {print \$4}'")"

  if [[ -z "${free_mb}" || "${free_mb}" -lt "${QPI_DEPLOY_MIN_FREE_MB}" ]]; then
    echo "Refusing deploy: only ${free_mb:-0} MB free under /opt/qpi/releases; need at least ${QPI_DEPLOY_MIN_FREE_MB} MB." >&2
    exit 1
  fi

  before_health_payload="$(remote_output "curl -fsS http://127.0.0.1:${BOT_HEALTH_PORT}/healthz || true")"

  if [[ "${before_service_state}" != "active" && "${QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY}" != "1" ]]; then
    echo "Refusing deploy: qpi-bot.service is '${before_service_state}'. Set QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY=1 for intentional recovery deploys." >&2
    exit 1
  fi
}

prune_runtime_artifacts() {
  require_nonnegative_integer "QPI_RUNTIME_ARTIFACT_RETENTION_COUNT" "${QPI_RUNTIME_ARTIFACT_RETENTION_COUNT}"
  require_nonnegative_integer "QPI_RUNTIME_ARTIFACT_RETENTION_DAYS" "${QPI_RUNTIME_ARTIFACT_RETENTION_DAYS}"

  mkdir -p "${artifacts_dir}"

  find "${artifacts_dir}" -maxdepth 1 -type f -name 'qpi-bot-*.tar.gz' \
    -mtime +"${QPI_RUNTIME_ARTIFACT_RETENTION_DAYS}" -delete

  mapfile -t runtime_archives < <(
    find "${artifacts_dir}" -maxdepth 1 -type f -name 'qpi-bot-*.tar.gz' -printf '%T@ %p\n' |
      sort -nr |
      awk '{sub(/^[^ ]+ /, ""); print}'
  )

  if (( ${#runtime_archives[@]} > QPI_RUNTIME_ARTIFACT_RETENTION_COUNT )); then
    for archive_path in "${runtime_archives[@]:QPI_RUNTIME_ARTIFACT_RETENTION_COUNT}"; do
      rm -f "${archive_path}"
    done
  fi
}

resolve_git_token
require_env "GH_TOKEN"
configure_yc_cli
prepare_ssh_key
verify_target_vm
remote_preflight

mkdir -p "${artifacts_dir}"
prune_runtime_artifacts
release_stamp="$(date -u +%Y%m%d%H%M%S)"
release_sha="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
release_id="${QPI_RELEASE_ID:-${release_stamp}-${release_sha}}"
archive_path="${artifacts_dir}/qpi-bot-${release_id}.tar.gz"

temp_dir="$(mktemp -d)"
overrides_env="${temp_dir}/qpi-bot-overrides.env"
rollout_env="${temp_dir}/qpi-rollout.env"

tar \
  --exclude=.git \
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude=.ruff_cache \
  --exclude=.mypy_cache \
  --exclude=.artifacts \
  --exclude=infra/.terraform \
  --exclude='infra/*.tfstate' \
  --exclude='infra/*.tfstate.*' \
  --exclude='infra/*.tfplan' \
  -czf "${archive_path}" \
  -C "${repo_root}" .

cat > "${overrides_env}" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TOKEN_CIPHER_KEY=${TOKEN_CIPHER_KEY}
WEBHOOK_SECRET_TOKEN=${BOT_WEBHOOK_SECRET_TOKEN}
ADMIN_TELEGRAM_IDS=${ADMIN_TELEGRAM_IDS:-}
SUPPORT_BOT_USERNAME=${SUPPORT_BOT_USERNAME:-}
EOF

cat > "${rollout_env}" <<EOF
GH_TOKEN=${GH_TOKEN}
TOKEN_YC_JSON_LOGGER=${GH_TOKEN}
EOF

install -m 0700 -d "${HOME}/.ssh"
touch "${HOME}/.ssh/known_hosts"
ssh-keyscan -p "${BOT_VM_SSH_PORT}" "${BOT_VM_HOST}" >> "${HOME}/.ssh/known_hosts" 2>/dev/null

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${archive_path}" \
  "${repo_root}/infra/scripts/remote_rollout_bot.sh" \
  "${repo_root}/infra/scripts/merge_bot_env.py" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/"

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${overrides_env}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/qpi-bot-overrides.env"

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${rollout_env}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/qpi-rollout.env"

schema_apply_decision="skipped"
if detect_schema_apply; then
  schema_apply_decision="applied"
  if ! command -v psqldef >/dev/null 2>&1; then
    echo "psqldef must be installed locally before runtime deploys that apply schema." >&2
    exit 1
  fi
  scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
    "$(command -v psqldef)" \
    "${repo_root}/infra/scripts/remote_apply_schema.sh" \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/"
fi

remote_exec \
  "sudo python3 /tmp/merge_bot_env.py \
    --base /etc/qpi/bot.env \
    --overrides /tmp/qpi-bot-overrides.env && \
   sudo chown root:${BOT_VM_SSH_USER} /etc/qpi/bot.env && \
   sudo chmod 0640 /etc/qpi/bot.env"

if [[ "${schema_apply_decision}" == "applied" ]]; then
  remote_exec \
    "chmod +x /tmp/psqldef /tmp/remote_apply_schema.sh && \
     /tmp/remote_apply_schema.sh '${release_id}' '/tmp/$(basename "${archive_path}")'"
fi

remote_exec \
  "set -a && source /tmp/qpi-rollout.env && set +a && \
   chmod +x /tmp/remote_rollout_bot.sh && \
   /tmp/remote_rollout_bot.sh '${release_id}' '/tmp/$(basename "${archive_path}")' '${BOT_HEALTH_PORT}'"

remote_exec \
  "set -a && source /etc/qpi/bot.env && set +a && \
   cd /opt/qpi/current && \
   ./.venv/bin/python -m services.bot_api.main \
     --seller-command '/start' \
     --telegram-id 910001 \
     --telegram-username deploy_smoke_seller && \
   ./.venv/bin/python -m services.bot_api.main \
     --buyer-command '/start' \
     --telegram-id 910002 \
     --telegram-username deploy_smoke_buyer"

after_release_target="$(remote_output "readlink -f /opt/qpi/current || true")"
after_service_state="$(remote_output "systemctl is-active qpi-bot.service || true")"
after_health_payload="$(remote_output "curl -fsS http://127.0.0.1:${BOT_HEALTH_PORT}/healthz")"

cat <<EOF
release_id=${release_id}
schema_apply=${schema_apply_decision}
before_release=${before_release_target}
after_release=${after_release_target}
before_service_state=${before_service_state}
after_service_state=${after_service_state}
free_mb_before=${free_mb}
before_health_payload=${before_health_payload}
after_health_payload=${after_health_payload}
EOF
