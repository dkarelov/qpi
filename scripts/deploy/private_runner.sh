#!/usr/bin/env bash
set -euo pipefail

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
  PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES (default: 30)
  PRIVATE_RUNNER_MAX_SESSION_MINUTES (default: 120)
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
PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES="${PRIVATE_RUNNER_IDLE_SHUTDOWN_MINUTES:-30}"
PRIVATE_RUNNER_MAX_SESSION_MINUTES="${PRIVATE_RUNNER_MAX_SESSION_MINUTES:-120}"
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

cancel_scheduled_shutdown() {
  remote_exec "sudo shutdown -c >/dev/null 2>&1 || true"
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

wait_for_runner_online() {
  local deadline=$((SECONDS + 600))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    if [[ "$(runner_online)" == "1" ]]; then
      return
    fi
    sleep 5
  done

  echo "Timed out waiting for GitHub runner '${PRIVATE_RUNNER_NAME}' to report online." >&2
  exit 1
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
    start_instance
    wait_for_ssh
    cancel_scheduled_shutdown
    install_or_reconfigure_runner
    wait_for_runner_online
    schedule_shutdown "${PRIVATE_RUNNER_MAX_SESSION_MINUTES}"
    print_status
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
