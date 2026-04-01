#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: detect_ci_changes.sh --event-name <event> [--base-sha <sha>] [--head-sha <sha>] [--force-full-validation]
EOF
}

event_name=""
base_sha=""
head_sha=""
force_full_validation=0

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
    --force-full-validation)
      force_full_validation=1
      shift
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

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

emit_output() {
  local needs_db_validation="$1"
  local requires_migration="$2"
  local has_runtime_changes="$3"
  local function_targets="$4"
  local has_function_targets="$5"
  local needs_private_runner="$6"
  local db_validation_mode="$7"
  local db_validation_targets="$8"

  printf 'needs_db_validation=%q\n' "${needs_db_validation}"
  printf 'requires_migration=%q\n' "${requires_migration}"
  printf 'has_runtime_changes=%q\n' "${has_runtime_changes}"
  printf 'function_targets=%q\n' "${function_targets}"
  printf 'has_function_targets=%q\n' "${has_function_targets}"
  printf 'needs_private_runner=%q\n' "${needs_private_runner}"
  printf 'db_validation_mode=%q\n' "${db_validation_mode}"
  printf 'db_validation_targets=%q\n' "${db_validation_targets}"
}

emit_full_output() {
  emit_output \
    "true" \
    "true" \
    "true" \
    "daily_report_scrapper order_tracker blockchain_checker" \
    "true" \
    "true" \
    "full" \
    ""
}

resolve_selection_from_paths() {
  (
    cd "${repo_root}"
    export PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
    python3 -m libs.devtools.validation_selection \
      --repo-root "${repo_root}" \
      --format shell \
      --paths "$@"
  )
}

require_command git
require_command python3

db_pytest_targets=""

if [[ "${force_full_validation}" -eq 1 ]]; then
  emit_full_output
  exit 0
fi

if [[ -z "${head_sha}" ]]; then
  if git rev-parse HEAD >/dev/null 2>&1; then
    head_sha="$(git rev-parse HEAD)"
  fi
fi

if [[ -z "${base_sha}" || "${base_sha}" == "0000000000000000000000000000000000000000" ]]; then
  if [[ -n "${head_sha}" ]] && git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
    base_sha="$(git rev-parse "${head_sha}^" 2>/dev/null || true)"
  fi
fi

if [[ -z "${base_sha}" || -z "${head_sha}" ]]; then
  emit_full_output
  exit 0
fi

if ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null || ! git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
  emit_full_output
  exit 0
fi

mapfile -t changed_files < <(git diff --name-only "${base_sha}" "${head_sha}")
if [[ "${#changed_files[@]}" -eq 0 ]]; then
  emit_output "false" "false" "false" "" "false" "false" "none" ""
  exit 0
fi

eval "$(resolve_selection_from_paths "${changed_files[@]}")"

emit_output \
  "${needs_db_validation}" \
  "${requires_migration}" \
  "${has_runtime_changes}" \
  "${function_targets}" \
  "${has_function_targets}" \
  "${needs_private_runner}" \
  "${db_validation_mode}" \
  "${db_pytest_targets}"
