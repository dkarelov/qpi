#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${QPI_PRIVATE_RUNNER_AUTOSHUTDOWN_ENV:-/etc/qpi/private-runner-autoshutdown.env}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi

RUNNER_DIR="${QPI_PRIVATE_RUNNER_RUNNER_DIR:-/opt/actions-runner}"
RUNNER_USER="${QPI_PRIVATE_RUNNER_RUNNER_USER:-github-runner}"
STATE_DIR="${QPI_PRIVATE_RUNNER_STATE_DIR:-/var/lib/qpi-private-runner}"
LAST_ACTIVITY_FILE="${QPI_PRIVATE_RUNNER_LAST_ACTIVITY_FILE:-${STATE_DIR}/last-activity}"
IDLE_MINUTES="${QPI_PRIVATE_RUNNER_IDLE_MINUTES:-60}"
SHUTDOWN_COMMAND="${QPI_PRIVATE_RUNNER_SHUTDOWN_COMMAND:-shutdown -h now}"

usage() {
  cat <<'EOF' >&2
usage:
  private_runner_autoshutdown.sh heartbeat
  private_runner_autoshutdown.sh idle-check
  private_runner_autoshutdown.sh status
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

command_name="$1"

ensure_state_dir() {
  install -d -m 0755 "${STATE_DIR}"
}

heartbeat() {
  ensure_state_dir
  touch "${LAST_ACTIVITY_FILE}"
}

runner_service_name() {
  if [[ -f "${RUNNER_DIR}/.service" ]]; then
    tr -d '\r\n' < "${RUNNER_DIR}/.service"
  fi
}

runner_service_state() {
  local unit_name="$1"
  if [[ -z "${unit_name}" ]]; then
    printf 'unknown\n'
    return
  fi
  if systemctl is-active --quiet "${unit_name}"; then
    printf 'active\n'
    return
  fi
  if systemctl is-failed --quiet "${unit_name}"; then
    printf 'failed\n'
    return
  fi
  printf 'inactive\n'
}

runner_worker_active() {
  if ! id -u "${RUNNER_USER}" >/dev/null 2>&1; then
    return 1
  fi
  pgrep -u "${RUNNER_USER}" -f 'Runner\.Worker' >/dev/null 2>&1
}

interactive_session_count() {
  who | awk 'NF { count += 1 } END { print count + 0 }'
}

last_activity_epoch() {
  if [[ -e "${LAST_ACTIVITY_FILE}" ]]; then
    stat -c %Y "${LAST_ACTIVITY_FILE}"
  fi
}

last_activity_iso() {
  local epoch="$1"
  if [[ -z "${epoch}" ]]; then
    printf 'never\n'
    return
  fi
  date -u -d "@${epoch}" +%Y-%m-%dT%H:%M:%SZ
}

status() {
  local runner_unit
  local runner_state
  local stamp_epoch=""
  local stamp_age="n/a"
  local now
  local sessions

  runner_unit="$(runner_service_name)"
  runner_state="$(runner_service_state "${runner_unit}")"
  sessions="$(interactive_session_count)"
  stamp_epoch="$(last_activity_epoch || true)"
  if [[ -n "${stamp_epoch}" ]]; then
    now="$(date +%s)"
    stamp_age="$((now - stamp_epoch))"
  fi
  local worker_active="0"
  if runner_worker_active; then
    worker_active="1"
  fi

  cat <<EOF
runner_dir=${RUNNER_DIR}
runner_user=${RUNNER_USER}
runner_service=${runner_unit:-unknown}
runner_service_state=${runner_state}
runner_worker_active=${worker_active}
interactive_sessions=${sessions}
idle_minutes=${IDLE_MINUTES}
state_dir=${STATE_DIR}
last_activity_file=${LAST_ACTIVITY_FILE}
last_activity_epoch=${stamp_epoch:-}
last_activity_iso=$(last_activity_iso "${stamp_epoch}")
last_activity_age_seconds=${stamp_age}
EOF
}

idle_check() {
  local sessions
  local idle_seconds
  local stamp_epoch
  local now
  local age_seconds

  ensure_state_dir
  sessions="$(interactive_session_count)"
  if runner_worker_active || [[ "${sessions}" -gt 0 ]]; then
    heartbeat
    exit 0
  fi

  if [[ ! -e "${LAST_ACTIVITY_FILE}" ]]; then
    heartbeat
    exit 0
  fi

  stamp_epoch="$(last_activity_epoch)"
  now="$(date +%s)"
  idle_seconds="$((IDLE_MINUTES * 60))"
  age_seconds="$((now - stamp_epoch))"
  if [[ "${age_seconds}" -lt "${idle_seconds}" ]]; then
    exit 0
  fi

  sessions="$(interactive_session_count)"
  if runner_worker_active || [[ "${sessions}" -gt 0 ]]; then
    heartbeat
    exit 0
  fi

  echo "Private runner idle for ${age_seconds}s (threshold ${idle_seconds}s); shutting down."
  eval "${SHUTDOWN_COMMAND}"
}

case "${command_name}" in
  heartbeat)
    heartbeat
    ;;
  idle-check)
    idle_check
    ;;
  status)
    status
    ;;
  *)
    usage
    exit 1
    ;;
esac
