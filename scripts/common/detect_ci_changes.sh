#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: detect_ci_changes.sh --event-name <event> [--base-sha <sha>] [--head-sha <sha>]
EOF
}

event_name=""
base_sha=""
head_sha=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --event-name)
      event_name="${2:-}"
      shift 2
      ;;
    --base-sha)
      base_sha="${2:-}"
      shift 2
      ;;
    --head-sha)
      head_sha="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${event_name}" ]]; then
  usage
  exit 1
fi

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

require_command git

matches_any() {
  local candidate="$1"
  shift
  local pattern
  for pattern in "$@"; do
    # shellcheck disable=SC2254
    case "${candidate}" in
      ${pattern})
        return 0
        ;;
    esac
  done
  return 1
}

emit_output() {
  local needs_db_validation="$1"
  local requires_migration="$2"
  local has_runtime_changes="$3"
  local function_targets="$4"
  local has_function_targets="$5"
  local needs_private_runner="$6"

  printf 'needs_db_validation=%q\n' "${needs_db_validation}"
  printf 'requires_migration=%q\n' "${requires_migration}"
  printf 'has_runtime_changes=%q\n' "${has_runtime_changes}"
  printf 'function_targets=%q\n' "${function_targets}"
  printf 'has_function_targets=%q\n' "${has_function_targets}"
  printf 'needs_private_runner=%q\n' "${needs_private_runner}"
}

all_function_targets="daily_report_scrapper order_tracker blockchain_checker"

if [[ "${event_name}" == "workflow_dispatch" ]]; then
  emit_output "true" "true" "true" "${all_function_targets}" "true" "true"
  exit 0
fi

if [[ -z "${base_sha}" || -z "${head_sha}" || "${base_sha}" == "0000000000000000000000000000000000000000" ]]; then
  emit_output "true" "true" "true" "${all_function_targets}" "true" "true"
  exit 0
fi

if ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null || ! git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
  emit_output "true" "true" "true" "${all_function_targets}" "true" "true"
  exit 0
fi

db_patterns=(
  "services/*"
  "libs/*"
  "tests/*"
  "schema/*"
  "pyproject.toml"
  "uv.lock"
  "requirements.txt"
  "scripts/dev/*"
)

migration_patterns=(
  "schema/*"
  "libs/db/*"
  "infra/scripts/remote_apply_schema.sh"
)

runtime_patterns=(
  "services/__init__.py"
  "services/bot_api/*"
  "libs/*"
  "schema/*"
  "pyproject.toml"
  "uv.lock"
  "requirements.txt"
  "scripts/common/setup_private_git_auth.sh"
  "scripts/deploy/runtime.sh"
  "infra/scripts/remote_apply_schema.sh"
  "infra/scripts/remote_rollout_bot.sh"
  "infra/scripts/merge_bot_env.py"
)

function_all_patterns=(
  "services/__init__.py"
  "libs/*"
  "pyproject.toml"
  "uv.lock"
  "requirements.txt"
  "scripts/common/setup_private_git_auth.sh"
  "scripts/deploy/function.sh"
)

needs_db_validation="false"
requires_migration="false"
has_runtime_changes="false"
has_function_targets="false"
function_targets=""

declare -A function_target_map=(
  ["daily_report_scrapper"]=0
  ["order_tracker"]=0
  ["blockchain_checker"]=0
)

while IFS= read -r changed_file; do
  [[ -n "${changed_file}" ]] || continue

  if matches_any "${changed_file}" "${migration_patterns[@]}"; then
    requires_migration="true"
  fi

  if matches_any "${changed_file}" "${runtime_patterns[@]}"; then
    has_runtime_changes="true"
    needs_db_validation="true"
  fi

  if matches_any "${changed_file}" "${function_all_patterns[@]}"; then
    function_target_map["daily_report_scrapper"]=1
    function_target_map["order_tracker"]=1
    function_target_map["blockchain_checker"]=1
    needs_db_validation="true"
  fi

  if matches_any "${changed_file}" "${db_patterns[@]}"; then
    needs_db_validation="true"
  fi

  if [[ "${changed_file}" == services/daily_report_scrapper/* ]]; then
    function_target_map["daily_report_scrapper"]=1
    needs_db_validation="true"
  fi

  if [[ "${changed_file}" == services/order_tracker/* ]]; then
    function_target_map["order_tracker"]=1
    needs_db_validation="true"
  fi

  if [[ "${changed_file}" == services/blockchain_checker/* ]]; then
    function_target_map["blockchain_checker"]=1
    needs_db_validation="true"
  fi
done < <(git diff --name-only "${base_sha}" "${head_sha}")

for target in daily_report_scrapper order_tracker blockchain_checker; do
  if [[ "${function_target_map[${target}]}" -eq 1 ]]; then
    if [[ -n "${function_targets}" ]]; then
      function_targets="${function_targets} "
    fi
    function_targets="${function_targets}${target}"
  fi
done

if [[ -n "${function_targets}" ]]; then
  has_function_targets="true"
fi

needs_private_runner="false"
if [[ "${needs_db_validation}" == "true" || "${has_runtime_changes}" == "true" || "${has_function_targets}" == "true" ]]; then
  needs_private_runner="true"
fi

emit_output "${needs_db_validation}" "${requires_migration}" "${has_runtime_changes}" "${function_targets}" "${has_function_targets}" "${needs_private_runner}"
