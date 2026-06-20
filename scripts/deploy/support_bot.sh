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
  SUPPORT_BOT_GROUP_ID
  SUPPORT_BOT_OWNER_ID
  SUPPORT_BOT_DATABASE_URL or DATABASE_URL
  TELEGRAM_API_PROXY_URLS

SSH auth:
  SUPPORT_BOT_VM_SSH_PRIVATE_KEY or SUPPORT_BOT_VM_SSH_KEY_PATH

Optional environment:
  SUPPORT_BOT_DEV_IDS
  SUPPORT_BOT_DB_SCHEMA (default: support_bot)
  SUPPORT_BOT_REDIS_DB (default: 7)
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
SUPPORT_BOT_DB_SCHEMA="${SUPPORT_BOT_DB_SCHEMA:-support_bot}"
SUPPORT_BOT_REDIS_DB="${SUPPORT_BOT_REDIS_DB:-7}"
TELEGRAM_API_PROXY_URLS="${TELEGRAM_API_PROXY_URLS:-}"

generated_ssh_key=0
ssh_key_path=""
temp_dir=""
support_bot_host=""
support_bot_database_url=""
ssh_args=()
scp_args=()

cleanup() {
  if [[ -n "${temp_dir}" && -d "${temp_dir}" ]]; then
    rm -rf "${temp_dir}"
  fi
  if [[ "${generated_ssh_key}" == "1" && -n "${ssh_key_path}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
  if [[ "${remote_uploaded:-0}" == "1" && -n "${support_bot_host:-}" && "${#ssh_args[@]}" -gt 0 ]]; then
    remote_exec "rm -f /tmp/remote_rollout_support_bot.sh /tmp/$(basename "${release_archive:-}")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

resolve_support_bot_database_url() {
  support_bot_database_url="${SUPPORT_BOT_DATABASE_URL:-${DATABASE_URL:-}}"
  if [[ -z "${support_bot_database_url}" ]]; then
    echo "SUPPORT_BOT_DATABASE_URL or DATABASE_URL is required." >&2
    exit 1
  fi
}

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

write_env_file() {
  local target="$1"

  {
    printf 'SUPPORT_BOT_IMAGE=%s\n' "${image_ref}"
    printf 'SUPPORT_BOT_TELEGRAM_BOT_TOKEN=%s\n' "${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}"
    printf 'SUPPORT_BOT_GROUP_ID=%s\n' "${SUPPORT_BOT_GROUP_ID}"
    printf 'SUPPORT_BOT_OWNER_ID=%s\n' "${SUPPORT_BOT_OWNER_ID}"
    printf 'SUPPORT_BOT_DEV_IDS=%s\n' "${SUPPORT_BOT_DEV_IDS:-}"
    printf 'DATABASE_URL=%s\n' "${support_bot_database_url}"
    printf 'SUPPORT_BOT_DB_SCHEMA=%s\n' "${SUPPORT_BOT_DB_SCHEMA}"
    printf 'REDIS_HOST=%s\n' "redis"
    printf 'REDIS_PORT=%s\n' "6379"
    printf 'REDIS_DB=%s\n' "${SUPPORT_BOT_REDIS_DB}"
    printf 'TELEGRAM_API_PROXY_URLS=%s\n' "${TELEGRAM_API_PROXY_URLS}"
    printf 'POLICY_ENABLED=%s\n' "${SUPPORT_BOT_POLICY_ENABLED:-false}"
    printf 'AI_PROVIDER=%s\n' "${SUPPORT_BOT_AI_PROVIDER:-none}"
  } > "${target}"
}

support_bot_telegram_get_me() {
  local telegram_get_me
  local telegram_username
  local proxy_url

  qpi_reject_legacy_telegram_api_proxy_url
  qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 1

  mapfile -t telegram_proxy_urls < <(qpi_parse_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}")
  for _round in 1 2 3; do
    for proxy_url in "${telegram_proxy_urls[@]}"; do
      if telegram_get_me="$(curl -fsS --connect-timeout 5 --max-time 15 --proxy "${proxy_url}" "https://api.telegram.org/bot${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}/getMe")" &&
        jq -e '.ok == true' >/dev/null <<<"${telegram_get_me}"; then
        telegram_username="$(jq -r '.result.username // "-"' <<<"${telegram_get_me}")"
        printf 'telegram_get_me_ok=%q\n' "true"
        printf 'telegram_get_me_username=%q\n' "${telegram_username}"
        return 0
      fi
    done
  done

  echo "Telegram getMe failed through TELEGRAM_API_PROXY_URLS." >&2
  return 1
}

support_bot_telegram_get_chat() {
  local chat_is_forum
  local chat_title
  local chat_type
  local proxy_url
  local telegram_get_chat

  qpi_reject_legacy_telegram_api_proxy_url
  qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 1

  mapfile -t telegram_proxy_urls < <(qpi_parse_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}")
  for _round in 1 2 3; do
    for proxy_url in "${telegram_proxy_urls[@]}"; do
      if telegram_get_chat="$(
        curl -fsS --connect-timeout 5 --max-time 15 --proxy "${proxy_url}" \
          --get --data-urlencode "chat_id=${SUPPORT_BOT_GROUP_ID}" \
          "https://api.telegram.org/bot${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}/getChat"
      )" && jq -e '.ok == true' >/dev/null <<<"${telegram_get_chat}"; then
        chat_type="$(jq -r '.result.type // "-"' <<<"${telegram_get_chat}")"
        chat_is_forum="$(jq -r '.result.is_forum // false' <<<"${telegram_get_chat}")"
        chat_title="$(jq -r '.result.title // "-"' <<<"${telegram_get_chat}")"
        if [[ "${chat_type}" == "supergroup" && "${chat_is_forum}" == "true" ]]; then
          printf 'telegram_get_chat_ok=%q\n' "true"
          printf 'telegram_get_chat_title=%q\n' "${chat_title}"
          printf 'telegram_get_chat_type=%q\n' "${chat_type}"
          printf 'telegram_get_chat_is_forum=%q\n' "${chat_is_forum}"
          return 0
        fi
        echo "SUPPORT_BOT_GROUP_ID must point to a topic-enabled supergroup; got type=${chat_type}, is_forum=${chat_is_forum}, title=${chat_title}." >&2
        return 1
      fi
    done
  done

  echo "Telegram getChat failed through TELEGRAM_API_PROXY_URLS. SUPPORT_BOT_GROUP_ID may be wrong or the bot may not be in the group." >&2
  return 1
}

support_bot_telegram_get_chat_member() {
  local bot_id
  local can_manage_topics
  local member_status
  local proxy_url
  local telegram_get_me
  local telegram_get_member

  qpi_reject_legacy_telegram_api_proxy_url
  qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 1

  mapfile -t telegram_proxy_urls < <(qpi_parse_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}")
  for _round in 1 2 3; do
    for proxy_url in "${telegram_proxy_urls[@]}"; do
      if telegram_get_me="$(curl -fsS --connect-timeout 5 --max-time 15 --proxy "${proxy_url}" "https://api.telegram.org/bot${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}/getMe")" &&
        jq -e '.ok == true' >/dev/null <<<"${telegram_get_me}"; then
        bot_id="$(jq -r '.result.id' <<<"${telegram_get_me}")"
        if telegram_get_member="$(
          curl -fsS --connect-timeout 5 --max-time 15 --proxy "${proxy_url}" \
            --get \
            --data-urlencode "chat_id=${SUPPORT_BOT_GROUP_ID}" \
            --data-urlencode "user_id=${bot_id}" \
            "https://api.telegram.org/bot${SUPPORT_BOT_TELEGRAM_BOT_TOKEN}/getChatMember"
        )" && jq -e '.ok == true' >/dev/null <<<"${telegram_get_member}"; then
          member_status="$(jq -r '.result.status // "-"' <<<"${telegram_get_member}")"
          can_manage_topics="$(jq -r '.result.can_manage_topics // false' <<<"${telegram_get_member}")"
          if [[ "${member_status}" == "administrator" && "${can_manage_topics}" == "true" ]]; then
            printf 'telegram_get_chat_member_ok=%q\n' "true"
            printf 'telegram_get_chat_member_status=%q\n' "${member_status}"
            printf 'telegram_get_chat_member_can_manage_topics=%q\n' "${can_manage_topics}"
            return 0
          fi
          echo "Support bot must be an administrator with can_manage_topics=true; got status=${member_status}, can_manage_topics=${can_manage_topics}." >&2
          return 1
        fi
      fi
    done
  done

  echo "Telegram getChatMember failed through TELEGRAM_API_PROXY_URLS. Check bot membership and administrator rights." >&2
  return 1
}

run_preflight() {
  eval "$("${script_dir}/preflight.sh" support-bot)"
}

qpi_timing_init

qpi_phase_start "preflight"
resolve_support_bot_database_url
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

mkdir -p "${release_stage}"
cp "${repo_root}/apps/support-bot/compose.prod.yml" "${release_stage}/compose.prod.yml"
write_env_file "${release_stage}/.env"
tar -czf "${release_archive}" -C "${release_stage}" .
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
remote_uploaded=1
qpi_phase_end

qpi_phase_start "rollout"
remote_exec \
  "set -euo pipefail && \
   sudo install -d -m 0755 /opt/support-bot/releases /var/lib/support-bot && \
   chmod +x /tmp/remote_rollout_support_bot.sh && \
   /tmp/remote_rollout_support_bot.sh '${release_id}' '/tmp/$(basename "${release_archive}")' '${image_ref}'"
qpi_phase_end

compose_command="sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml"

qpi_phase_start "smoke"
support_bot_redis_ping="$(
  remote_output "${compose_command} exec -T redis redis-cli ping"
)"
if [[ "${support_bot_redis_ping}" != "PONG" ]]; then
  echo "Redis PING failed: ${support_bot_redis_ping}" >&2
  exit 1
fi

postgres_status="$(
  remote_output \
    "${compose_command} exec -T supportbot uv run --no-sync python -c 'exec(\"\"\"import asyncio
import os

import asyncpg

from app.bot.storage import create_schema

async def main():
    pool = await asyncpg.create_pool(os.environ[\"DATABASE_URL\"], min_size=1, max_size=1)
    try:
        await create_schema(pool, os.environ.get(\"SUPPORT_BOT_DB_SCHEMA\", \"support_bot\"))
    finally:
        await pool.close()
    print(\"support_bot_postgres_ok=true\")

asyncio.run(main())
\"\"\")'"
)"

telegram_get_me_output="$(support_bot_telegram_get_me)"
telegram_get_chat_output="$(support_bot_telegram_get_chat)"
telegram_get_chat_member_output="$(support_bot_telegram_get_chat_member)"
qpi_phase_end

qpi_phase_start "cleanup-old-mongo"
old_mongo_cleanup="$(
  remote_output \
    "if sudo test -d /var/lib/support-bot/mongodb; then sudo rm -rf /var/lib/support-bot/mongodb && echo old_mongo_deleted=true; else echo old_mongo_deleted=absent; fi"
)"
qpi_phase_end

echo "release_id=${release_id}"
echo "support_bot_host=${support_bot_host}"
echo "image_ref=${image_ref}"
echo "support_bot_redis_ping=${support_bot_redis_ping}"
echo "${postgres_status}"
echo "${telegram_get_me_output}"
echo "${telegram_get_chat_output}"
echo "${telegram_get_chat_member_output}"
echo "${old_mongo_cleanup}"

qpi_append_step_summary "### Support Bot Deploy Result"
qpi_append_step_summary ""
qpi_append_step_summary "- Release ID: \`${release_id}\`"
qpi_append_step_summary "- Image: \`${image_ref}\`"
qpi_append_step_summary "- Redis ping: \`${support_bot_redis_ping}\`"
qpi_append_step_summary "- PostgreSQL schema: \`ok\`"
qpi_append_step_summary "- Telegram getMe via proxy: \`ok\`"
qpi_append_step_summary "- Telegram forum group validation: \`ok\`"
qpi_append_step_summary "- Telegram bot topic-admin validation: \`ok\`"
qpi_append_step_summary "- Old Mongo state cleanup: \`${old_mongo_cleanup}\`"
qpi_append_step_summary ""
qpi_emit_timing_summary "Support Bot Deploy"
