#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF' >&2
usage:
  private_runner.sh ensure-ready
  private_runner.sh schedule-stop
  private_runner.sh stop-now
  private_runner.sh status

Required environment:
  YC_FOLDER_ID
  PRIVATE_RUNNER_INSTANCE_NAME

For ensure-ready / schedule-stop:
  PRIVATE_RUNNER_SSH_PRIVATE_KEY or PRIVATE_RUNNER_SSH_KEY_PATH

For ensure-ready:
  PRIVATE_RUNNER_REPO
  PRIVATE_RUNNER_BOOTSTRAP_TOKEN

Optional environment:
  PRIVATE_RUNNER_SSH_USER (default: ubuntu)
  PRIVATE_RUNNER_SSH_PORT (default: 22)
  PRIVATE_RUNNER_NAME (default: PRIVATE_RUNNER_INSTANCE_NAME)
  PRIVATE_RUNNER_LABELS (default: qpi-private,qpi-deploy)
  PRIVATE_RUNNER_SYSTEM_USER (default: github-runner)
  PRIVATE_RUNNER_INSTALL_DIR (default: /opt/actions-runner)
  PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES (default: 60)
  PRIVATE_RUNNER_ONLINE_TIMEOUT_SECONDS (default: 150)
  PRIVATE_RUNNER_VERSION (default: 2.330.0)
  MIN_PRIVATE_RUNNER_VERSION (default: 2.329.0)
  PRIVATE_RUNNER_FORCE_RECONFIGURE (default: 0)
  GITHUB_API_URL (default: https://api.github.com)
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

command_name="$1"

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

require_command yc
require_command curl
require_command python3
require_env "YC_FOLDER_ID"
require_env "PRIVATE_RUNNER_INSTANCE_NAME"
configure_yc_cli

PRIVATE_RUNNER_SSH_USER="${PRIVATE_RUNNER_SSH_USER:-ubuntu}"
PRIVATE_RUNNER_SSH_PORT="${PRIVATE_RUNNER_SSH_PORT:-22}"
PRIVATE_RUNNER_NAME="${PRIVATE_RUNNER_NAME:-${PRIVATE_RUNNER_INSTANCE_NAME}}"
PRIVATE_RUNNER_LABELS="${PRIVATE_RUNNER_LABELS:-qpi-private,qpi-deploy}"
PRIVATE_RUNNER_SYSTEM_USER="${PRIVATE_RUNNER_SYSTEM_USER:-github-runner}"
PRIVATE_RUNNER_INSTALL_DIR="${PRIVATE_RUNNER_INSTALL_DIR:-/opt/actions-runner}"
PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES="${PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES:-60}"
PRIVATE_RUNNER_ONLINE_TIMEOUT_SECONDS="${PRIVATE_RUNNER_ONLINE_TIMEOUT_SECONDS:-150}"
PRIVATE_RUNNER_VERSION="${PRIVATE_RUNNER_VERSION:-2.330.0}"
MIN_PRIVATE_RUNNER_VERSION="${MIN_PRIVATE_RUNNER_VERSION:-2.329.0}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

version_lt() {
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" != "$2" ]]
}

if version_lt "${PRIVATE_RUNNER_VERSION}" "${MIN_PRIVATE_RUNNER_VERSION}"; then
  echo "PRIVATE_RUNNER_VERSION=${PRIVATE_RUNNER_VERSION} is too old; requires ${MIN_PRIVATE_RUNNER_VERSION}+." >&2
  exit 1
fi

prepare_ssh_key() {
  local key_source
  if [[ -n "${PRIVATE_RUNNER_SSH_PRIVATE_KEY:-}" ]]; then
    ssh_key_path="$(mktemp)"
    chmod 600 "${ssh_key_path}"
    printf '%s' "${PRIVATE_RUNNER_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      printf '%b' "${PRIVATE_RUNNER_SSH_PRIVATE_KEY}" > "${ssh_key_path}"
    fi
    if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
      if ! printf '%s' "${PRIVATE_RUNNER_SSH_PRIVATE_KEY}" | base64 -d > "${ssh_key_path}" 2>/dev/null; then
        :
      fi
    fi
    sed -i 's/\r$//' "${ssh_key_path}"
    generated_ssh_key=1
  else
    key_source="${PRIVATE_RUNNER_SSH_KEY_PATH:-${HOME}/.ssh/id_rsa}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    ssh_key_path="${key_source}"
    generated_ssh_key=0
  fi

  if ! ssh-keygen -y -f "${ssh_key_path}" >/dev/null 2>&1; then
    echo "Failed to decode PRIVATE_RUNNER_SSH_PRIVATE_KEY into a valid private key." >&2
    exit 1
  fi
}

cleanup() {
  if [[ "${generated_ssh_key:-0}" == "1" && -n "${ssh_key_path:-}" && -f "${ssh_key_path}" ]]; then
    rm -f "${ssh_key_path}"
  fi
}
trap cleanup EXIT

instance_json() {
  yc compute instance get \
    --folder-id "${YC_FOLDER_ID}" \
    --name "${PRIVATE_RUNNER_INSTANCE_NAME}" \
    --format json
}

instance_field() {
  local field="$1"
  instance_json | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
field = sys.argv[1]

def get(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None

def public_ip(data):
    interfaces = get(data, "network_interfaces", "networkInterfaces") or []
    for interface in interfaces:
        primary = get(interface, "primary_v4_address", "primaryV4Address") or {}
        one_to_one = get(primary, "one_to_one_nat", "oneToOneNat") or {}
        address = get(one_to_one, "address")
        if address:
            return address
    return ""

def private_ip(data):
    interfaces = get(data, "network_interfaces", "networkInterfaces") or []
    for interface in interfaces:
        primary = get(interface, "primary_v4_address", "primaryV4Address") or {}
        address = get(primary, "address")
        if address:
            return address
    return ""

value = {
    "id": get(payload, "id") or "",
    "status": get(payload, "status") or "",
    "public_ip": public_ip(payload),
    "private_ip": private_ip(payload),
}.get(field, "")
print(value)
' "$field"
}

github_api() {
  local method="$1"
  local path="$2"
  shift 2
  curl -fsSL \
    -X "${method}" \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${PRIVATE_RUNNER_BOOTSTRAP_TOKEN}" \
    "${GITHUB_API_URL%/}${path}" \
    "$@"
}

runner_registration_token() {
  require_env "PRIVATE_RUNNER_REPO"
  require_env "PRIVATE_RUNNER_BOOTSTRAP_TOKEN"
  github_api POST "/repos/${PRIVATE_RUNNER_REPO}/actions/runners/registration-token" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
}

runner_record_json() {
  require_env "PRIVATE_RUNNER_REPO"
  require_env "PRIVATE_RUNNER_BOOTSTRAP_TOKEN"
  github_api GET "/repos/${PRIVATE_RUNNER_REPO}/actions/runners?per_page=100" |
    python3 -c '
import json
import sys

runner_name = sys.argv[1]
payload = json.load(sys.stdin)
for runner in payload.get("runners", []):
    if runner.get("name") == runner_name:
        print(json.dumps(runner))
        break
' "${PRIVATE_RUNNER_NAME}"
}

runner_exists() {
  [[ -n "$(runner_record_json)" ]]
}

runner_online() {
  runner_record_json | python3 -c '
import json
import sys

payload = sys.stdin.read().strip()
if not payload:
    print("0")
    raise SystemExit(0)

runner = json.loads(payload)
print("1" if runner.get("status") == "online" else "0")
'
}

sync_autoshutdown_controller() {
  local local_hash
  local remote_hash

  local_hash="$(sha256sum "${repo_root}/scripts/deploy/private_runner_autoshutdown.sh" | cut -d' ' -f1)"
  remote_hash="$(remote_exec "bash -s" <<'REMOTE'
set -euo pipefail
sudo shutdown -c >/dev/null 2>&1 || true
if [[ -x /usr/local/bin/qpi-private-runner-autoshutdown ]]; then
  sudo /usr/local/bin/qpi-private-runner-autoshutdown heartbeat >/dev/null 2>&1 || true
  sha256sum /usr/local/bin/qpi-private-runner-autoshutdown | cut -d" " -f1
fi
REMOTE
)"

  if [[ "${remote_hash}" == "${local_hash}" ]]; then
    echo "autoshutdown controller up to date."
    return 0
  fi
  echo "autoshutdown controller missing or stale; refreshing."
  install_or_refresh_autoshutdown_controller
  autoshutdown_heartbeat
}

dump_diagnostics() {
  echo "--- private runner diagnostics ---"
  echo "instance_status: $(instance_field status 2>/dev/null || echo unavailable)"
  local record
  record="$(runner_record_json 2>/dev/null || true)"
  echo "github_runner_record: ${record:-<absent>}"
  remote_exec "bash -s" <<'REMOTE' || echo "remote diagnostics unavailable (SSH failed)."
set -uo pipefail
runner_unit=""
if [[ -f /opt/actions-runner/.service ]]; then
  runner_unit="$(tr -d '\r\n' < /opt/actions-runner/.service)"
fi
echo "runner_unit: ${runner_unit:-<none>}"
ls -l /opt/actions-runner/.runner /opt/actions-runner/.credentials 2>/dev/null || echo "runner agent not configured"
if [[ -n "${runner_unit}" ]]; then
  sudo systemctl status --no-pager "${runner_unit}" 2>&1 | head -n 20
  sudo journalctl -u "${runner_unit}" -n 50 --no-pager 2>&1 | tail -n 50
fi
echo "autoshutdown_timer: $(systemctl is-enabled qpi-private-runner-autoshutdown.timer 2>&1)"
REMOTE
  echo "--- end private runner diagnostics ---"
}

start_instance() {
  local status
  status="$(instance_field status)"
  case "${status}" in
    RUNNING)
      return
      ;;
    STARTING)
      ;;
    *)
      yc compute instance start \
        --folder-id "${YC_FOLDER_ID}" \
        --name "${PRIVATE_RUNNER_INSTANCE_NAME}" \
        >/dev/null
      ;;
  esac

  local deadline=$((SECONDS + 600))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    status="$(instance_field status)"
    if [[ "${status}" == "RUNNING" ]]; then
      return
    fi
    sleep 5
  done

  echo "Timed out waiting for runner instance to enter RUNNING state." >&2
  exit 1
}

remote_exec() {
  local public_ip
  public_ip="$(instance_field public_ip)"
  if [[ -z "${public_ip}" ]]; then
    echo "Runner public IP is not available." >&2
    exit 1
  fi

  ssh \
    -p "${PRIVATE_RUNNER_SSH_PORT}" \
    -i "${ssh_key_path}" \
    -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    "${PRIVATE_RUNNER_SSH_USER}@${public_ip}" \
    "$@"
}

wait_for_ssh() {
  local deadline=$((SECONDS + 600))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    if remote_exec "true" >/dev/null 2>&1; then
      return
    fi
    sleep 5
  done

  echo "Timed out waiting for SSH on runner instance." >&2
  exit 1
}

autoshutdown_env_content() {
  cat <<EOF
QPI_PRIVATE_RUNNER_IDLE_MINUTES=${PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES}
QPI_PRIVATE_RUNNER_RUNNER_DIR=${PRIVATE_RUNNER_INSTALL_DIR}
QPI_PRIVATE_RUNNER_RUNNER_USER=${PRIVATE_RUNNER_SYSTEM_USER}
QPI_PRIVATE_RUNNER_STATE_DIR=/var/lib/qpi-private-runner
EOF
}

autoshutdown_service_content() {
  cat <<'EOF'
[Unit]
Description=QPI private runner idle shutdown check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/qpi-private-runner-autoshutdown idle-check
EOF
}

autoshutdown_timer_content() {
  cat <<'EOF'
[Unit]
Description=Run QPI private runner idle shutdown checks

[Timer]
OnBootSec=1m
OnUnitActiveSec=1m
Persistent=true
Unit=qpi-private-runner-autoshutdown.service

[Install]
WantedBy=timers.target
EOF
}

install_or_reconfigure_runner() {
  require_env "PRIVATE_RUNNER_REPO"
  require_env "PRIVATE_RUNNER_BOOTSTRAP_TOKEN"

  local registration_token
  local should_reconfigure="0"

  if [[ "${PRIVATE_RUNNER_FORCE_RECONFIGURE:-0}" == "1" ]]; then
    should_reconfigure="1"
  elif ! runner_exists; then
    should_reconfigure="1"
  elif ! remote_exec "test -f $(printf '%q' "${PRIVATE_RUNNER_INSTALL_DIR}")/.credentials"; then
    # A recreated VM loses its on-disk registration even though the GitHub
    # runner record persists; re-register under the same name.
    should_reconfigure="1"
  fi

  if [[ "${should_reconfigure}" == "1" ]]; then
    registration_token="$(runner_registration_token)"
  else
    registration_token=""
  fi

  local remote_command
  remote_command="$(
    printf \
      'RUNNER_DIR=%q RUNNER_USER=%q RUNNER_VERSION=%q RUNNER_REPO=%q RUNNER_NAME=%q RUNNER_LABELS=%q REGISTRATION_TOKEN=%q RECONFIGURE=%q bash -s' \
      "${PRIVATE_RUNNER_INSTALL_DIR}" \
      "${PRIVATE_RUNNER_SYSTEM_USER}" \
      "${PRIVATE_RUNNER_VERSION}" \
      "${PRIVATE_RUNNER_REPO}" \
      "${PRIVATE_RUNNER_NAME}" \
      "${PRIVATE_RUNNER_LABELS}" \
      "${registration_token}" \
      "${should_reconfigure}"
  )"

  remote_exec "${remote_command}" <<'REMOTE'
set -euo pipefail

sudo install -m 0755 -d "${RUNNER_DIR}"
if ! id -u "${RUNNER_USER}" >/dev/null 2>&1; then
  sudo useradd --create-home --shell /bin/bash "${RUNNER_USER}"
fi
sudo chown -R "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_DIR}"

if [[ ! -x "${RUNNER_DIR}/run.sh" ]]; then
  archive="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  tmp_archive="/tmp/${archive}"
  curl -fsSL -o "${tmp_archive}" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${archive}"
  sudo -u "${RUNNER_USER}" tar -xzf "${tmp_archive}" -C "${RUNNER_DIR}"
  if [[ -x "${RUNNER_DIR}/bin/installdependencies.sh" ]]; then
    sudo "${RUNNER_DIR}/bin/installdependencies.sh"
  fi
fi

if [[ "${RECONFIGURE}" == "1" ]]; then
  if [[ -x "${RUNNER_DIR}/svc.sh" ]]; then
    (
      cd "${RUNNER_DIR}"
      sudo ./svc.sh stop >/dev/null 2>&1 || true
      sudo ./svc.sh uninstall >/dev/null 2>&1 || true
    )
  fi
  sudo rm -f \
    "${RUNNER_DIR}/.runner" \
    "${RUNNER_DIR}/.credentials" \
    "${RUNNER_DIR}/.credentials_rsaparams" \
    "${RUNNER_DIR}/.service"

  sudo -u "${RUNNER_USER}" "${RUNNER_DIR}/config.sh" \
    --unattended \
    --replace \
    --url "https://github.com/${RUNNER_REPO}" \
    --token "${REGISTRATION_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}"
  sudo test -f "${RUNNER_DIR}/.credentials_rsaparams"
  (
    cd "${RUNNER_DIR}"
    sudo ./svc.sh install "${RUNNER_USER}"
  )
fi

(
  cd "${RUNNER_DIR}"
  sudo ./svc.sh start
)
REMOTE
}

install_or_refresh_autoshutdown_controller() {
  require_command base64

  local script_b64
  local env_b64
  local service_b64
  local timer_b64
  local remote_command

  script_b64="$(base64 < "${repo_root}/scripts/deploy/private_runner_autoshutdown.sh" | tr -d '\n')"
  env_b64="$(autoshutdown_env_content | base64 | tr -d '\n')"
  service_b64="$(autoshutdown_service_content | base64 | tr -d '\n')"
  timer_b64="$(autoshutdown_timer_content | base64 | tr -d '\n')"

  remote_command="$(
    printf \
      'SCRIPT_B64=%q ENV_B64=%q SERVICE_B64=%q TIMER_B64=%q bash -s' \
      "${script_b64}" \
      "${env_b64}" \
      "${service_b64}" \
      "${timer_b64}"
  )"

  remote_exec "${remote_command}" <<'REMOTE'
set -euo pipefail

sudo install -d -m 0755 /etc/qpi /var/lib/qpi-private-runner
printf '%s' "${SCRIPT_B64}" | base64 -d | sudo tee /usr/local/bin/qpi-private-runner-autoshutdown >/dev/null
printf '%s' "${ENV_B64}" | base64 -d | sudo tee /etc/qpi/private-runner-autoshutdown.env >/dev/null
printf '%s' "${SERVICE_B64}" | base64 -d | sudo tee /etc/systemd/system/qpi-private-runner-autoshutdown.service >/dev/null
printf '%s' "${TIMER_B64}" | base64 -d | sudo tee /etc/systemd/system/qpi-private-runner-autoshutdown.timer >/dev/null

sudo chmod 0755 /usr/local/bin/qpi-private-runner-autoshutdown
sudo chmod 0644 /etc/qpi/private-runner-autoshutdown.env
sudo chmod 0644 /etc/systemd/system/qpi-private-runner-autoshutdown.service
sudo chmod 0644 /etc/systemd/system/qpi-private-runner-autoshutdown.timer
sudo chown root:root \
  /usr/local/bin/qpi-private-runner-autoshutdown \
  /etc/qpi/private-runner-autoshutdown.env \
  /etc/systemd/system/qpi-private-runner-autoshutdown.service \
  /etc/systemd/system/qpi-private-runner-autoshutdown.timer

sudo systemctl daemon-reload
sudo systemctl enable --now qpi-private-runner-autoshutdown.timer
REMOTE
}

autoshutdown_heartbeat() {
  remote_exec "sudo /usr/local/bin/qpi-private-runner-autoshutdown heartbeat"
}

wait_for_runner_online() {
  local timeout_seconds="$1"
  local interval_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    if [[ "$(runner_online)" == "1" ]]; then
      return 0
    fi
    sleep "${interval_seconds}"
  done
  return 1
}

schedule_shutdown() {
  local minutes="${1:-${PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES}}"
  remote_exec "sudo shutdown -h +${minutes}"
}

stop_now() {
  yc compute instance stop \
    --folder-id "${YC_FOLDER_ID}" \
    --name "${PRIVATE_RUNNER_INSTANCE_NAME}" \
    >/dev/null
}

print_status() {
  cat <<EOF
instance_name=${PRIVATE_RUNNER_INSTANCE_NAME}
instance_id=$(instance_field id)
status=$(instance_field status)
public_ip=$(instance_field public_ip)
private_ip=$(instance_field private_ip)
runner_name=${PRIVATE_RUNNER_NAME}
runner_registered=$([[ -n "${PRIVATE_RUNNER_BOOTSTRAP_TOKEN:-}" && -n "${PRIVATE_RUNNER_REPO:-}" ]] && runner_exists && echo 1 || echo 0)
runner_online=$([[ -n "${PRIVATE_RUNNER_BOOTSTRAP_TOKEN:-}" && -n "${PRIVATE_RUNNER_REPO:-}" ]] && [[ "$(runner_online)" == "1" ]] && echo 1 || echo 0)
EOF
}

case "${command_name}" in
  ensure-ready)
    prepare_ssh_key
    qpi_timing_init

    qpi_phase_start "instance_start"
    start_instance
    qpi_phase_end

    # Housekeeping (legacy shutdown cancel, autoshutdown heartbeat/refresh) runs
    # over SSH in parallel with the runner-online poll; the baked systemd units
    # bring the runner agent up at boot without any SSH on the happy path.
    housekeeping_log="$(mktemp)"
    (wait_for_ssh && sync_autoshutdown_controller) >"${housekeeping_log}" 2>&1 &
    housekeeping_pid="$!"

    runner_is_online=0
    if [[ "${PRIVATE_RUNNER_FORCE_RECONFIGURE:-0}" != "1" ]] && runner_exists; then
      qpi_phase_start "runner_online"
      if wait_for_runner_online "${PRIVATE_RUNNER_ONLINE_TIMEOUT_SECONDS}" 3; then
        runner_is_online=1
      fi
      qpi_phase_end
    fi

    if [[ "${runner_is_online}" != "1" ]]; then
      echo "Runner is not online after instance start; attempting reconfigure." >&2
      dump_diagnostics >&2 || true
      qpi_phase_start "reconfigure"
      if ! wait "${housekeeping_pid}"; then
        echo "Housekeeping failed before reconfigure:" >&2
        cat "${housekeeping_log}" >&2
      fi
      housekeeping_pid=""
      install_or_reconfigure_runner
      qpi_phase_end
      qpi_phase_start "runner_online_retry"
      if ! wait_for_runner_online 300 5; then
        dump_diagnostics >&2 || true
        echo "Timed out waiting for GitHub runner '${PRIVATE_RUNNER_NAME}' to report online." >&2
        exit 1
      fi
      qpi_phase_end
    fi

    if [[ -n "${housekeeping_pid}" ]]; then
      if ! wait "${housekeeping_pid}"; then
        cat "${housekeeping_log}" >&2
        echo "Runner housekeeping (shutdown cancel / autoshutdown sync) failed." >&2
        exit 1
      fi
    fi
    cat "${housekeeping_log}"
    rm -f "${housekeeping_log}"

    print_status
    qpi_emit_timing_summary "Private Runner Ensure-Ready"
    ;;
  schedule-stop)
    if [[ "$(instance_field status)" != "RUNNING" ]]; then
      echo "Runner instance is not running; nothing to schedule."
      exit 0
    fi
    prepare_ssh_key
    wait_for_ssh
    schedule_shutdown "${PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES}"
    ;;
  stop-now)
    stop_now
    ;;
  status)
    if [[ -n "${PRIVATE_RUNNER_BOOTSTRAP_TOKEN:-}" && -n "${PRIVATE_RUNNER_REPO:-}" ]]; then
      print_status
    else
      cat <<EOF
instance_name=${PRIVATE_RUNNER_INSTANCE_NAME}
instance_id=$(instance_field id)
status=$(instance_field status)
public_ip=$(instance_field public_ip)
private_ip=$(instance_field private_ip)
runner_name=${PRIVATE_RUNNER_NAME}
EOF
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
