#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
artifacts_dir="${repo_root}/.artifacts/runtime"

# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF' >&2
usage:
  runtime.sh build
  runtime.sh metadata
  runtime.sh deploy [archive-path]
  runtime.sh [archive-path]

Required environment for deploy:
  BOT_VM_HOST
  TELEGRAM_BOT_TOKEN
  TOKEN_CIPHER_KEY
  BOT_WEBHOOK_SECRET_TOKEN
  YC_FOLDER_ID
  GH_TOKEN or TOKEN_YC_JSON_LOGGER

Optional environment:
  BOT_VM_INSTANCE_GROUP_NAME (default: qpi-bot-ig)
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_HEALTH_PORT (default: 18080)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  ADMIN_TELEGRAM_IDS
  SUPPORT_BOT_USERNAME
  DEPLOY_BASE_SHA / DEPLOY_HEAD_SHA (for schema auto-detection)
  QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY (default: 0)
  QPI_DEPLOY_SCHEMA_MODE (default: auto)
  QPI_DEPLOY_MIN_FREE_MB (default: 2048)
  QPI_PREDEPLOY_ONLY (default: 0)
  QPI_RELEASE_ID
  QPI_RUNTIME_ARCHIVE_PATH
  QPI_RUNTIME_ARTIFACT_RETENTION_COUNT (default: 10)
  QPI_RUNTIME_ARTIFACT_RETENTION_DAYS (default: 14)
EOF
}

command_name="deploy"
archive_arg="${QPI_RUNTIME_ARCHIVE_PATH:-}"

case "${1:-}" in
  build|metadata|deploy)
    command_name="$1"
    shift
    ;;
  --help)
    usage
    exit 0
    ;;
esac

if [[ $# -gt 1 ]]; then
  usage
  exit 1
fi

if [[ $# -eq 1 ]]; then
  archive_arg="$1"
fi

BOT_VM_INSTANCE_GROUP_NAME="${BOT_VM_INSTANCE_GROUP_NAME:-qpi-bot-ig}"
BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}"
BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}"
BOT_HEALTH_PORT="${BOT_HEALTH_PORT:-18080}"
QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY="${QPI_ALLOW_DEPLOY_WHEN_UNHEALTHY:-0}"
QPI_DEPLOY_MIN_FREE_MB="${QPI_DEPLOY_MIN_FREE_MB:-2048}"
QPI_PREDEPLOY_ONLY="${QPI_PREDEPLOY_ONLY:-0}"
QPI_RUNTIME_ARTIFACT_RETENTION_COUNT="${QPI_RUNTIME_ARTIFACT_RETENTION_COUNT:-10}"
QPI_RUNTIME_ARTIFACT_RETENTION_DAYS="${QPI_RUNTIME_ARTIFACT_RETENTION_DAYS:-14}"

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

cleanup() {
  if [[ -n "${temp_dir:-}" && -d "${temp_dir}" ]]; then
    rm -rf "${temp_dir}"
  fi
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

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
  qpi_require_nonnegative_integer "QPI_RUNTIME_ARTIFACT_RETENTION_COUNT" "${QPI_RUNTIME_ARTIFACT_RETENTION_COUNT}"
  qpi_require_nonnegative_integer "QPI_RUNTIME_ARTIFACT_RETENTION_DAYS" "${QPI_RUNTIME_ARTIFACT_RETENTION_DAYS}"

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

build_archive() {
  local release_stamp
  local release_sha
  local release_id
  local archive_path

  mkdir -p "${artifacts_dir}"
  prune_runtime_artifacts

  release_stamp="$(date -u +%Y%m%d%H%M%S)"
  release_sha="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
  release_id="${QPI_RELEASE_ID:-${release_stamp}-${release_sha}}"
  archive_path="${artifacts_dir}/qpi-bot-${release_id}.tar.gz"

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

  printf '%s\n' "${archive_path}"
}

emit_metadata() {
  local bundle_path
  local sha256
  bundle_path="$(build_archive)"
  sha256="$(sha256sum "${bundle_path}" | awk '{print $1}')"
  python3 - <<'PY' "${bundle_path}" "${sha256}"
import json
import sys

print(json.dumps({"archive_path": sys.argv[1], "sha256": sys.argv[2]}))
PY
}

run_preflight() {
  eval "$("${script_dir}/preflight.sh" runtime)"
}

if [[ "${command_name}" == "build" ]]; then
  build_archive
  exit 0
fi

if [[ "${command_name}" == "metadata" ]]; then
  emit_metadata
  exit 0
fi

resolve_git_token
qpi_require_env "BOT_VM_HOST"
qpi_require_env "TELEGRAM_BOT_TOKEN"
qpi_require_env "TOKEN_CIPHER_KEY"
qpi_require_env "BOT_WEBHOOK_SECRET_TOKEN"
qpi_require_env "YC_FOLDER_ID"
qpi_require_env "GH_TOKEN"

generated_ssh_key=0
ssh_key_path=""
temp_dir="$(mktemp -d)"

qpi_timing_init

qpi_phase_start "preflight"
run_preflight
runtime_schema_action="${runtime_schema_action:-assert-clean}"
qpi_phase_end

qpi_phase_start "package"
runtime_archive_path="${archive_arg:-}"
if [[ -z "${runtime_archive_path}" ]]; then
  runtime_archive_path="$(build_archive)"
fi
runtime_archive_sha256="$(sha256sum "${runtime_archive_path}" | awk '{print $1}')"
qpi_phase_end

if [[ "${QPI_PREDEPLOY_ONLY}" == "1" ]]; then
  echo "release_id=${QPI_RELEASE_ID:-}"
  echo "runtime_archive_path=${runtime_archive_path}"
  echo "runtime_archive_sha256=${runtime_archive_sha256}"
  echo "schema_apply=${runtime_schema_action}"
  qpi_emit_timing_summary "Runtime Deploy"
  exit 0
fi

qpi_prepare_private_key "BOT_VM_SSH_PRIVATE_KEY" "BOT_VM_SSH_KEY_PATH" "${HOME}/.ssh/id_rsa" ssh_key_path generated_ssh_key

overrides_env="${temp_dir}/qpi-bot-overrides.env"
rollout_env="${temp_dir}/qpi-rollout.env"

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

qpi_phase_start "remote_preflight"
remote_preflight
qpi_phase_end

qpi_phase_start "upload"
install -m 0700 -d "${HOME}/.ssh"
touch "${HOME}/.ssh/known_hosts"
ssh-keyscan -p "${BOT_VM_SSH_PORT}" "${BOT_VM_HOST}" >> "${HOME}/.ssh/known_hosts" 2>/dev/null

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${runtime_archive_path}" \
  "${repo_root}/infra/scripts/remote_rollout_bot.sh" \
  "${repo_root}/infra/scripts/merge_bot_env.py" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/"

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${overrides_env}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/qpi-bot-overrides.env"

scp -P "${BOT_VM_SSH_PORT}" -i "${ssh_key_path}" \
  "${rollout_env}" \
  "${BOT_VM_SSH_USER}@${BOT_VM_HOST}:/tmp/qpi-rollout.env"
qpi_phase_end

qpi_phase_start "schema"
remote_exec \
  "sudo python3 /tmp/merge_bot_env.py \
    --base /etc/qpi/bot.env \
    --overrides /tmp/qpi-bot-overrides.env && \
   sudo chown root:${BOT_VM_SSH_USER} /etc/qpi/bot.env && \
   sudo chmod 0640 /etc/qpi/bot.env"

if [[ "${runtime_schema_action}" == "apply" ]]; then
  BOT_VM_HOST="${BOT_VM_HOST}" \
  BOT_VM_SSH_USER="${BOT_VM_SSH_USER}" \
  BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT}" \
  BOT_VM_SSH_PRIVATE_KEY="${BOT_VM_SSH_PRIVATE_KEY:-}" \
  BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-}" \
  "${repo_root}/scripts/deploy/schema_remote.sh" apply
else
  BOT_VM_HOST="${BOT_VM_HOST}" \
  BOT_VM_SSH_USER="${BOT_VM_SSH_USER}" \
  BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT}" \
  BOT_VM_SSH_PRIVATE_KEY="${BOT_VM_SSH_PRIVATE_KEY:-}" \
  BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-}" \
  "${repo_root}/scripts/deploy/schema_remote.sh" assert-clean
fi
qpi_phase_end

qpi_phase_start "rollout"
release_id="${QPI_RELEASE_ID:-$(basename "${runtime_archive_path}" .tar.gz | sed 's/^qpi-bot-//')}"
remote_exec \
  "set -a && source /tmp/qpi-rollout.env && set +a && \
   chmod +x /tmp/remote_rollout_bot.sh && \
   /tmp/remote_rollout_bot.sh '${release_id}' '/tmp/$(basename "${runtime_archive_path}")' '${BOT_HEALTH_PORT}'"
qpi_phase_end

qpi_phase_start "smoke"
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
qpi_phase_end

echo "release_id=${release_id}"
echo "runtime_archive_path=${runtime_archive_path}"
echo "runtime_archive_sha256=${runtime_archive_sha256}"
echo "schema_apply=${runtime_schema_action}"
echo "before_release=${before_release_target}"
echo "after_release=${after_release_target}"
echo "before_service_state=${before_service_state}"
echo "after_service_state=${after_service_state}"
echo "free_mb_before=${free_mb}"
echo "before_health_payload=${before_health_payload}"
echo "after_health_payload=${after_health_payload}"

qpi_append_step_summary "### Runtime Deploy Result"
qpi_append_step_summary ""
qpi_append_step_summary "- Release ID: \`${release_id}\`"
qpi_append_step_summary "- Archive SHA256: \`${runtime_archive_sha256}\`"
qpi_append_step_summary "- Schema action: \`${runtime_schema_action}\`"
qpi_append_step_summary "- Service state: \`${after_service_state}\`"
qpi_append_step_summary ""
qpi_emit_timing_summary "Runtime Deploy"
