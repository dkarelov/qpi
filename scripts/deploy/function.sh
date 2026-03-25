#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cache_root="${repo_root}/.artifacts/function-bundles"
python_platform="${QPI_FUNCTION_PYTHON_PLATFORM:-x86_64-manylinux_2_17}"
python_version="${QPI_FUNCTION_PYTHON_VERSION:-3.12}"

usage() {
  cat <<'EOF' >&2
usage:
  function.sh deploy <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh metadata <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh build <daily_report_scrapper|order_tracker|blockchain_checker>
  function.sh <daily_report_scrapper|order_tracker|blockchain_checker>
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

  manifest_hash="$(bundle_manifest_hash)"
  bundle_dir="${cache_root}/${service_name}"
  stage_dir="${bundle_dir}/stage-${manifest_hash}"
  bundle_path="${bundle_dir}/${manifest_hash}.zip"
  requirements_path="${bundle_dir}/${manifest_hash}.requirements.txt"
  staged_requirements_path="${stage_dir}/requirements.txt"

  mkdir -p "${bundle_dir}"
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

latest_version_id() {
  local versions_json
  versions_json="$(mktemp)"
  trap 'rm -f "${versions_json}"' RETURN

  yc serverless function version list \
    --function-name "${function_name}" \
    --limit 20 \
    --format json > "${versions_json}"

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
}

build_version_create_args() {
  local version_id="$1"
  local version_json

  version_json="$(mktemp)"
  trap 'rm -f "${version_json}"' RETURN
  yc serverless function version get "${version_id}" --format json > "${version_json}"

  python3 - "${version_json}" <<'PY'
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

deploy_bundle() {
  local bundle_path
  local version_id
  local description
  local -a create_args

  bundle_path="$(build_bundle)"
  version_id="$(latest_version_id)"
  description="Direct code-only deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mapfile -d '' -t create_args < <(build_version_create_args "${version_id}")

  yc serverless function version create \
    --function-name "${function_name}" \
    --source-path "${bundle_path}" \
    "${create_args[@]}" \
    --description "${description}"
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
