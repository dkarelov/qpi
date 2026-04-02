#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cache_root="${repo_root}/.artifacts/function-bundles"

usage() {
  cat <<'EOF' >&2
usage:
  function.sh deploy <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh metadata <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh build <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh <daily_report_scrapper|order_tracker|blockchain_checker>

Required environment for deploy:
  YC_FOLDER_ID
  BOT_VM_HOST

Required environment for build/metadata/deploy:
  GH_TOKEN or TOKEN_YC_JSON_LOGGER

Optional environment:
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  QPI_FUNCTION_BUNDLE_RETENTION_COUNT (default: 10)
  QPI_FUNCTION_BUNDLE_RETENTION_DAYS (default: 14)
EOF
}

command_name="deploy"
if [[ "${1:-}" == "deploy" || "${1:-}" == "metadata" || "${1:-}" == "build" ]]; then
  command_name="$1"
  shift
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

service_name="$1"
QPI_FUNCTION_BUNDLE_RETENTION_COUNT="${QPI_FUNCTION_BUNDLE_RETENTION_COUNT:-10}"
QPI_FUNCTION_BUNDLE_RETENTION_DAYS="${QPI_FUNCTION_BUNDLE_RETENTION_DAYS:-14}"

case "${service_name}" in
  daily_report_scrapper)
    function_name="${QPI_DAILY_REPORT_SCRAPPER_FUNCTION_NAME:-qpi-daily-report-scrapper}"
    ;;
  order_tracker)
    function_name="${QPI_ORDER_TRACKER_FUNCTION_NAME:-qpi-order-tracker}"
    ;;
  blockchain_checker)
    function_name="${QPI_BLOCKCHAIN_CHECKER_FUNCTION_NAME:-qpi-blockchain-checker}"
    ;;
  *)
    usage
    exit 1
    ;;
esac

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required." >&2
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

bundle_manifest_hash() {
  (
    cd "${repo_root}"
    {
      printf '%s\0' "pyproject.toml" "uv.lock" "services/__init__.py" "scripts/deploy/function.sh"
      find "libs" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0
      find "services/${service_name}" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0
    } | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
  )
}

build_bundle() {
  local manifest_hash
  local bundle_dir
  local stage_dir
  local bundle_path
  local requirements_path
  local staged_requirements_path

  resolve_git_token
  require_env "GH_TOKEN"

  manifest_hash="$(bundle_manifest_hash)"
  bundle_dir="${cache_root}/${service_name}"
  stage_dir="${bundle_dir}/stage-${manifest_hash}"
  bundle_path="${bundle_dir}/${manifest_hash}.zip"
  requirements_path="${bundle_dir}/${manifest_hash}.requirements.txt"
  staged_requirements_path="${stage_dir}/requirements.txt"

  mkdir -p "${bundle_dir}"
  prune_bundle_dir "${bundle_dir}"
  if [[ -f "${bundle_path}" && -f "${bundle_path}.sha256" ]]; then
    printf '%s\n' "${bundle_path}"
    return
  fi

  rm -rf "${stage_dir}"
  mkdir -p "${stage_dir}/services"

  uv export \
    --project "${repo_root}" \
    --frozen \
    --no-dev \
    --no-editable \
    --no-emit-project \
    --no-hashes \
    --output-file "${requirements_path}" >&2
  sed "s#https://github.com/#https://x-access-token:${GH_TOKEN}@github.com/#g" \
    "${requirements_path}" > "${staged_requirements_path}"

  cp -R "${repo_root}/libs" "${stage_dir}/libs"
  cp "${repo_root}/services/__init__.py" "${stage_dir}/services/__init__.py"
  cp -R "${repo_root}/services/${service_name}" "${stage_dir}/services/${service_name}"

  (
    cd "${stage_dir}"
    zip -qr "${bundle_path}" .
  )
  sha256sum "${bundle_path}" | awk '{print $1}' > "${bundle_path}.sha256"
  rm -rf "${stage_dir}"

  printf '%s\n' "${bundle_path}"
}

prune_bundle_hash() {
  local bundle_dir="$1"
  local bundle_hash="$2"

  rm -f \
    "${bundle_dir}/${bundle_hash}.zip" \
    "${bundle_dir}/${bundle_hash}.zip.sha256" \
    "${bundle_dir}/${bundle_hash}.requirements.txt"
  rm -rf "${bundle_dir}/stage-${bundle_hash}"
}

prune_bundle_dir() {
  local bundle_dir="$1"

  require_nonnegative_integer "QPI_FUNCTION_BUNDLE_RETENTION_COUNT" "${QPI_FUNCTION_BUNDLE_RETENTION_COUNT}"
  require_nonnegative_integer "QPI_FUNCTION_BUNDLE_RETENTION_DAYS" "${QPI_FUNCTION_BUNDLE_RETENTION_DAYS}"

  mapfile -t old_hashes < <(
    find "${bundle_dir}" -maxdepth 1 -type f -name '*.zip' -mtime +"${QPI_FUNCTION_BUNDLE_RETENTION_DAYS}" -printf '%f\n' |
      sed 's/\.zip$//'
  )
  for bundle_hash in "${old_hashes[@]}"; do
    prune_bundle_hash "${bundle_dir}" "${bundle_hash}"
  done

  find "${bundle_dir}" -maxdepth 1 -type d -name 'stage-*' -mtime +"${QPI_FUNCTION_BUNDLE_RETENTION_DAYS}" \
    -exec rm -rf {} +

  mapfile -t live_hashes < <(
    find "${bundle_dir}" -maxdepth 1 -type f -name '*.zip' -printf '%T@ %f\n' |
      sort -nr |
      awk '{sub(/^[^ ]+ /, ""); sub(/\.zip$/, ""); print}'
  )

  if (( ${#live_hashes[@]} > QPI_FUNCTION_BUNDLE_RETENTION_COUNT )); then
    for bundle_hash in "${live_hashes[@]:QPI_FUNCTION_BUNDLE_RETENTION_COUNT}"; do
      prune_bundle_hash "${bundle_dir}" "${bundle_hash}"
    done
  fi
}

latest_version_id() {
  local versions_json
  local version_id
  versions_json="$(mktemp)"

  yc serverless function version list \
    --folder-id "${YC_FOLDER_ID}" \
    --function-name "${function_name}" \
    --limit 20 \
    --format json > "${versions_json}"

  version_id="$(
    python3 - "${versions_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)
if isinstance(payload, dict):
    items = payload.get("versions") or payload.get("items") or []
else:
    items = payload

if not items:
    raise SystemExit("No live function versions found; provision the function with Terraform first.")

def created_at(item):
    return item.get("created_at") or item.get("createdAt") or ""

latest = max(items, key=created_at)
version_id = latest.get("id")
if not version_id:
    raise SystemExit("Failed to resolve the latest function version id from yc output.")

print(version_id)
PY
  )"
  rm -f "${versions_json}"
  printf '%s\n' "${version_id}"
}

build_version_create_args() {
  local version_id="$1"
  local version_json
  local args_file

  version_json="$(mktemp)"
  args_file="$(mktemp)"
  yc serverless function version get "${version_id}" --format json > "${version_json}"

  python3 - "${version_json}" > "${args_file}" <<'PY'
import json
import sys


def get(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None


with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)

args: list[str] = []


def add(flag: str, value):
    if value in (None, "", [], {}):
        return
    args.extend([flag, str(value)])


def format_memory(value):
    if value in (None, ""):
        return None
    text = str(value)
    if text.isdigit():
        raw = int(text)
        if raw % (1024 * 1024) == 0:
            return f"{raw // (1024 * 1024)}MB"
    return text


add("--runtime", get(payload, "runtime"))
add("--entrypoint", get(payload, "entrypoint"))
resources = get(payload, "resources") or {}
add("--memory", format_memory(get(resources, "memory")))
add("--execution-timeout", get(payload, "execution_timeout", "executionTimeout"))
add("--service-account-id", get(payload, "service_account_id", "serviceAccountId"))

environment = get(payload, "environment") or {}
if environment:
    add(
        "--environment",
        ",".join(f"{key}={environment[key]}" for key in sorted(environment)),
    )

connectivity = get(payload, "connectivity") or {}
add("--network-id", get(connectivity, "network_id", "networkId"))

log_options = get(payload, "log_options", "logOptions") or {}
add("--log-group-id", get(log_options, "log_group_id", "logGroupId"))
add("--concurrency", get(payload, "concurrency"))

tags = [tag for tag in (get(payload, "tags") or []) if tag and tag != "$latest"]
if tags:
    add("--tags", ",".join(tags))

for arg in args:
    sys.stdout.buffer.write(arg.encode("utf-8"))
    sys.stdout.buffer.write(b"\0")
PY
  rm -f "${version_json}"
  cat "${args_file}"
  rm -f "${args_file}"
}

emit_metadata() {
  local bundle_path
  local sha256
  bundle_path="$(build_bundle)"
  sha256="$(cat "${bundle_path}.sha256")"
  python3 - <<'PY' "${bundle_path}" "${sha256}"
import json
import sys

print(json.dumps({"zip_path": sys.argv[1], "sha256": sys.argv[2]}))
PY
}

compare_version_configs() {
  local old_version_id="$1"
  local new_version_id="$2"
  local old_json
  local new_json
  local compare_status=0

  old_json="$(mktemp)"
  new_json="$(mktemp)"

  yc serverless function version get "${old_version_id}" --format json > "${old_json}"
  yc serverless function version get "${new_version_id}" --format json > "${new_json}"

  if ! python3 - "${old_json}" "${new_json}" <<'PY'
import json
import sys


def get(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None


def normalize(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    resources = get(payload, "resources") or {}
    connectivity = get(payload, "connectivity") or {}
    log_options = get(payload, "log_options", "logOptions") or {}
    environment = get(payload, "environment") or {}
    return {
        "runtime": get(payload, "runtime"),
        "entrypoint": get(payload, "entrypoint"),
        "memory": get(resources, "memory"),
        "timeout": get(payload, "execution_timeout", "executionTimeout"),
        "service_account_id": get(payload, "service_account_id", "serviceAccountId"),
        "environment": dict(sorted(environment.items())),
        "network_id": get(connectivity, "network_id", "networkId"),
        "log_group_id": get(log_options, "log_group_id", "logGroupId"),
        "concurrency": get(payload, "concurrency"),
    }


old = normalize(sys.argv[1])
new = normalize(sys.argv[2])
if old != new:
    print("Critical function config drift detected.", file=sys.stderr)
    print("Expected:", file=sys.stderr)
    print(json.dumps(old, ensure_ascii=True, indent=2, sort_keys=True), file=sys.stderr)
    print("Actual:", file=sys.stderr)
    print(json.dumps(new, ensure_ascii=True, indent=2, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)
PY
  then
    compare_status=1
  fi
  rm -f "${old_json}" "${new_json}"
  return "${compare_status}"
}

deploy_bundle() {
  local bundle_path
  local bundle_size
  local version_id
  local description
  local created_json
  local created_version_id
  local -a create_args

  resolve_git_token
  require_env "GH_TOKEN"
  require_env "YC_FOLDER_ID"
  require_env "BOT_VM_HOST"
  configure_yc_cli

  BOT_VM_HOST="${BOT_VM_HOST}" \
  BOT_VM_SSH_USER="${BOT_VM_SSH_USER:-ubuntu}" \
  BOT_VM_SSH_PORT="${BOT_VM_SSH_PORT:-22}" \
  BOT_VM_SSH_PRIVATE_KEY="${BOT_VM_SSH_PRIVATE_KEY:-}" \
  BOT_VM_SSH_KEY_PATH="${BOT_VM_SSH_KEY_PATH:-}" \
  "${repo_root}/scripts/deploy/schema_remote.sh" assert-clean

  bundle_path="$(build_bundle)"
  bundle_size="$(wc -c < "${bundle_path}")"
  version_id="$(latest_version_id)"
  description="Direct code-only deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mapfile -d '' -t create_args < <(build_version_create_args "${version_id}")

  echo "Bundle: ${bundle_path}"
  echo "Bundle size (bytes): ${bundle_size}"
  echo "Source live version: ${version_id}"

  created_json="$(mktemp)"
  yc serverless function version create \
    --folder-id "${YC_FOLDER_ID}" \
    --function-name "${function_name}" \
    --source-path "${bundle_path}" \
    "${create_args[@]}" \
    --description "${description}" \
    --format json > "${created_json}"

  created_version_id="$(
    python3 - "${created_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)
version_id = payload.get("id")
if not version_id:
    raise SystemExit("Failed to resolve created function version id from yc output.")
print(version_id)
PY
  )"
  rm -f "${created_json}"

  echo "Created function version: ${created_version_id}"
  compare_version_configs "${version_id}" "${created_version_id}"
}

case "${command_name}" in
  build)
    build_bundle
    ;;
  metadata)
    emit_metadata
    ;;
  deploy)
    deploy_bundle
    ;;
  *)
    usage
    exit 1
    ;;
esac
