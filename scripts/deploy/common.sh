#!/usr/bin/env bash

qpi_require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required." >&2
    exit 1
  fi
}

qpi_require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

qpi_require_nonnegative_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${name} must be a non-negative integer." >&2
    exit 1
  fi
}

qpi_configure_yc_cli() {
  if [[ -n "${YC_TOKEN:-}" ]]; then
    yc config set token "${YC_TOKEN}" >/dev/null
  fi
  if [[ -n "${YC_FOLDER_ID:-}" ]]; then
    yc config set folder-id "${YC_FOLDER_ID}" >/dev/null
  fi
}

qpi_prepare_private_key() {
  local secret_var_name="$1"
  local key_path_var_name="$2"
  local default_key_path="$3"
  local output_var_name="$4"
  local generated_var_name="$5"

  local secret_value="${!secret_var_name:-}"
  local key_source=""
  local prepared_key_path=""
  local generated_key="0"

  if [[ -n "${secret_value}" ]]; then
    prepared_key_path="$(mktemp)"
    chmod 600 "${prepared_key_path}"
    printf '%s' "${secret_value}" > "${prepared_key_path}"
    if ! ssh-keygen -y -f "${prepared_key_path}" >/dev/null 2>&1; then
      printf '%b' "${secret_value}" > "${prepared_key_path}"
    fi
    if ! ssh-keygen -y -f "${prepared_key_path}" >/dev/null 2>&1; then
      if ! printf '%s' "${secret_value}" | base64 -d > "${prepared_key_path}" 2>/dev/null; then
        :
      fi
    fi
    sed -i 's/\r$//' "${prepared_key_path}"
    generated_key="1"
  else
    key_source="${!key_path_var_name:-${default_key_path}}"
    if [[ ! -f "${key_source}" ]]; then
      echo "SSH key not found: ${key_source}" >&2
      exit 1
    fi
    prepared_key_path="${key_source}"
  fi

  if ! ssh-keygen -y -f "${prepared_key_path}" >/dev/null 2>&1; then
    echo "Failed to decode ${secret_var_name} into a valid private key." >&2
    exit 1
  fi

  printf -v "${output_var_name}" '%s' "${prepared_key_path}"
  printf -v "${generated_var_name}" '%s' "${generated_key}"
}

qpi_verify_host_in_instance_group() {
  local folder_id="$1"
  local instance_group_name="$2"
  local expected_host="$3"

  yc compute instance-group list-instances \
    --folder-id "${folder_id}" \
    --name "${instance_group_name}" \
    --format json | python3 -c '
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
    interfaces = item.get("network_interfaces", []) or item.get("networkInterfaces", [])
    for interface in interfaces:
        primary = interface.get("primary_v4_address") or interface.get("primaryV4Address") or {}
        addresses = [primary.get("address")]
        nat = primary.get("one_to_one_nat") or primary.get("oneToOneNat") or {}
        addresses.append(nat.get("address"))
        if expected in [address for address in addresses if address]:
            raise SystemExit(0)

raise SystemExit(1)
' "${expected_host}"
}

qpi_resolve_support_bot_host() {
  local folder_id="$1"
  local instance_group_name="$2"

  yc compute instance-group list-instances \
    --folder-id "${folder_id}" \
    --name "${instance_group_name}" \
    --format json | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
items = payload if isinstance(payload, list) else (payload.get("instances") or payload.get("items") or [])
if not items:
    raise SystemExit(1)

item = items[0]
interfaces = item.get("network_interfaces", []) or item.get("networkInterfaces", [])
for interface in interfaces:
    primary = interface.get("primary_v4_address") or interface.get("primaryV4Address") or {}
    address = primary.get("address")
    if address:
        print(address)
        raise SystemExit(0)

raise SystemExit(1)
'
}

qpi_detect_runtime_schema_action() {
  local schema_mode="${QPI_DEPLOY_SCHEMA_MODE:-auto}"
  local base_ref="${DEPLOY_BASE_SHA:-}"
  local head_ref="${DEPLOY_HEAD_SHA:-}"
  local diff_target=""

  case "${schema_mode}" in
    always)
      printf '%s\n' "apply"
      return 0
      ;;
    never)
      printf '%s\n' "assert-clean"
      return 0
      ;;
    auto)
      ;;
    *)
      echo "Unsupported QPI_DEPLOY_SCHEMA_MODE: ${schema_mode}" >&2
      exit 1
      ;;
  esac

  if [[ -n "${base_ref}" && -n "${head_ref}" ]]; then
    if git cat-file -e "${base_ref}^{commit}" 2>/dev/null && git cat-file -e "${head_ref}^{commit}" 2>/dev/null; then
      diff_target="$(git diff --name-only "${base_ref}" "${head_ref}")"
    else
      printf '%s\n' "apply"
      return 0
    fi
  elif [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
    printf '%s\n' "apply"
    return 0
  elif git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    diff_target="$(git diff --name-only HEAD^ HEAD)"
  else
    printf '%s\n' "apply"
    return 0
  fi

  if printf '%s\n' "${diff_target}" | grep -Eq '^(schema/|libs/db/|scripts/deploy/schema_remote\.sh$)'; then
    printf '%s\n' "apply"
  else
    printf '%s\n' "assert-clean"
  fi
}

qpi_timing_init() {
  QPI_TIMING_STARTED_AT="$(date +%s)"
  QPI_ACTIVE_PHASE=""
  QPI_ACTIVE_PHASE_STARTED_AT=""
  QPI_TIMING_RECORDS=()
}

qpi_phase_start() {
  QPI_ACTIVE_PHASE="$1"
  QPI_ACTIVE_PHASE_STARTED_AT="$(date +%s)"
}

qpi_phase_end() {
  local phase_ended_at
  local duration_seconds

  if [[ -z "${QPI_ACTIVE_PHASE:-}" || -z "${QPI_ACTIVE_PHASE_STARTED_AT:-}" ]]; then
    return 0
  fi

  phase_ended_at="$(date +%s)"
  duration_seconds="$((phase_ended_at - QPI_ACTIVE_PHASE_STARTED_AT))"
  QPI_TIMING_RECORDS+=("${QPI_ACTIVE_PHASE}:${duration_seconds}")
  QPI_ACTIVE_PHASE=""
  QPI_ACTIVE_PHASE_STARTED_AT=""
}

qpi_append_step_summary() {
  if [[ -z "${GITHUB_STEP_SUMMARY:-}" ]]; then
    return 0
  fi
  printf '%s\n' "$1" >> "${GITHUB_STEP_SUMMARY}"
}

qpi_emit_timing_summary() {
  local title="$1"
  local total_seconds
  local record
  local phase_name
  local phase_seconds

  qpi_phase_end
  total_seconds="$(( $(date +%s) - QPI_TIMING_STARTED_AT ))"

  printf 'timing_title=%q\n' "${title}"
  printf 'timing_total_seconds=%q\n' "${total_seconds}"
  for record in "${QPI_TIMING_RECORDS[@]}"; do
    phase_name="${record%%:*}"
    phase_seconds="${record##*:}"
    printf 'timing_phase_%s_seconds=%q\n' "${phase_name}" "${phase_seconds}"
  done

  qpi_append_step_summary "## ${title}"
  qpi_append_step_summary ""
  qpi_append_step_summary "| Phase | Seconds |"
  qpi_append_step_summary "| --- | ---: |"
  for record in "${QPI_TIMING_RECORDS[@]}"; do
    phase_name="${record%%:*}"
    phase_seconds="${record##*:}"
    qpi_append_step_summary "| \`${phase_name}\` | ${phase_seconds} |"
  done
  qpi_append_step_summary "| \`total\` | ${total_seconds} |"
  qpi_append_step_summary ""
}
