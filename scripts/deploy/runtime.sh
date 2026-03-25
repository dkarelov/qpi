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

Optional environment:
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_HEALTH_PORT (default: 18080)
  ADMIN_TELEGRAM_IDS
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  GH_TOKEN or TOKEN_YC_JSON_LOGGER
  DEPLOY_BASE_SHA / DEPLOY_HEAD_SHA (for schema auto-detection)
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

require_env "BOT_VM_HOST"
require_env "TELEGRAM_BOT_TOKEN"
require_env "TOKEN_CIPHER_KEY"
require_env "BOT_WEBHOOK_SECRET_TOKEN"

BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
BOT_HEALTH_PORT="${BOT_HEALTH_PORT:-18080}"

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

resolve_git_token
require_env "GH_TOKEN"

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
      printf '%s' "${BOT_VM_SSH_PRIVATE_KEY}" | base64 -d > "${ssh_key_path}"
    fi
    sed -i 's/\r$//' "${ssh_key_path}"
  else
    key_source="${BOT_VM_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    ssh_key_path="${key_source}"
  fi
  ssh-keygen -y -f "${ssh_key_path}" >/dev/null
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

generated_ssh_key=0
prepare_ssh_key
if [[ -n "${BOT_VM_SSH_PRIVATE_KEY:-}" ]]; then
  generated_ssh_key=1
fi

mkdir -p "${artifacts_dir}"
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

if detect_schema_apply; then
  if ! command -v psqldef >/dev/null 2>&1; then
    echo "psqldef must be installed locally before runtime deploys that apply schema." >&2
    exit 1
  fi
  scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
    "$(command -v psqldef)" \
    "${repo_root}/infra/scripts/remote_apply_schema.sh" \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/"
fi

ssh -p "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
  "sudo python3 /tmp/merge_bot_env.py \
    --base /etc/qpi/bot.env \
    --overrides /tmp/qpi-bot-overrides.env && \
   sudo chown root:${BOT_VM_SSH_USER} /etc/qpi/bot.env && \
   sudo chmod 0640 /etc/qpi/bot.env"

if detect_schema_apply; then
  ssh -p "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
    "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
    "chmod +x /tmp/psqldef /tmp/remote_apply_schema.sh && \
     /tmp/remote_apply_schema.sh '${release_id}' '/tmp/$(basename "${archive_path}")'"
fi

ssh -p "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
  "set -a && source /tmp/qpi-rollout.env && set +a && \
   chmod +x /tmp/remote_rollout_bot.sh && \
   /tmp/remote_rollout_bot.sh '${release_id}' '/tmp/$(basename "${archive_path}")' '${BOT_HEALTH_PORT}'"

ssh -p "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}" \
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
