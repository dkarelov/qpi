#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cache_root="${repo_root}/.artifacts/function-bundles"

# shellcheck source=scripts/deploy/common.sh
source "${script_dir}/common.sh"

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
  GH_TOKEN or TOKEN_YC_JSON_LOGGER

Optional environment:
  BOT_VM_SSH_USER (default: ubuntu)
  BOT_VM_SSH_PORT (default: 22)
  BOT_VM_SSH_KEY_PATH (default: ~/.ssh/id_rsa)
  BOT_VM_SSH_PRIVATE_KEY
  QPI_FUNCTION_BUNDLE_PATH
  QPI_PREDEPLOY_ONLY (default: 0)
  QPI_SKIP_FUNCTION_SCHEMA_CHECK (default: 0)
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
QPI_PREDEPLOY_ONLY="${QPI_PREDEPLOY_ONLY:-0}"
QPI_SKIP_FUNCTION_SCHEMA_CHECK="${QPI_SKIP_FUNCTION_SCHEMA_CHECK:-0}"

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

prune_bundle_hash() {
  local bundle_dir="$1"
  local bundle_hash="$2"

  rm -f \
    "${bundle_dir}/${bundle_hash}.zip" \
    "${bundle_dir}/${bundle_hash}.zip.sha256" \
    "${bundle_dir}/${bundle_hash}.direct-url-requirements.txt" \
    "${bundle_dir}/${bundle_hash}.requirements.txt"
  rm -rf "${bundle_dir}/stage-${bundle_hash}"
}

prune_bundle_dir() {
  local bundle_dir="$1"

  qpi_require_nonnegative_integer "QPI_FUNCTION_BUNDLE_RETENTION_COUNT" "${QPI_FUNCTION_BUNDLE_RETENTION_COUNT}"
  qpi_require_nonnegative_integer "QPI_FUNCTION_BUNDLE_RETENTION_DAYS" "${QPI_FUNCTION_BUNDLE_RETENTION_DAYS}"

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

build_bundle() {
  local manifest_hash
  local bundle_dir
  local stage_dir
  local bundle_path
  local direct_requirements_path
  local requirements_path
  local staged_requirements_path
  local wheels_dir

  resolve_git_token
  qpi_require_env "GH_TOKEN"
  "${repo_root}/scripts/common/setup_private_git_auth.sh" >&2

  manifest_hash="$(bundle_manifest_hash)"
  bundle_dir="${cache_root}/${service_name}"
  stage_dir="${bundle_dir}/stage-${manifest_hash}"
  bundle_path="${bundle_dir}/${manifest_hash}.zip"
  direct_requirements_path="${bundle_dir}/${manifest_hash}.direct-url-requirements.txt"
  requirements_path="${bundle_dir}/${manifest_hash}.requirements.txt"
  staged_requirements_path="${stage_dir}/requirements.txt"
  wheels_dir="${stage_dir}/vendor/wheels"

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

  mkdir -p "${wheels_dir}"
  python3 - "${requirements_path}" "${direct_requirements_path}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

requirements_path = Path(sys.argv[1])
direct_path = Path(sys.argv[2])

direct_lines = [
    line
    for line in requirements_path.read_text(encoding="utf-8").splitlines()
    if " @ git+" in line.strip() and not line.strip().startswith("#")
]
direct_path.write_text("\n".join(direct_lines) + ("\n" if direct_lines else ""), encoding="utf-8")
PY
  if [[ -s "${direct_requirements_path}" ]]; then
    python3 -m pip wheel --no-cache-dir --requirement "${direct_requirements_path}" --wheel-dir "${wheels_dir}" >&2
  fi

  python3 - "${requirements_path}" "${staged_requirements_path}" "${wheels_dir}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

requirements_path = Path(sys.argv[1])
staged_path = Path(sys.argv[2])
wheels_dir = Path(sys.argv[3])


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


wheel_versions: dict[str, str] = {}
for wheel in wheels_dir.glob("*.whl"):
    parts = wheel.name.split("-")
    if len(parts) >= 2:
        wheel_versions[normalize_name(parts[0])] = parts[1]

lines = ["--no-index", "--find-links ./vendor/wheels"]
for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
    stripped = raw_line.strip()
    if " @ git+" in stripped and not stripped.startswith("#"):
        package_name = stripped.split(" @ ", 1)[0].strip()
        version = wheel_versions.get(normalize_name(package_name))
        if version is None:
            raise SystemExit(f"Missing local wheel for direct URL requirement: {package_name}")
        lines.append(f"{package_name}=={version}")
        continue
    lines.append(raw_line)

payload = "\n".join(lines)
if "x-access-token" in payload or "GH_TOKEN" in payload:
    raise SystemExit("Generated function requirements contain secret markers")
staged_path.write_text(payload + "\n", encoding="utf-8")
PY

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
items = payload if isinstance(payload, list) else (payload.get("versions") or payload.get("items") or [])
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

compare_version_configs() {
  local old_version_id="$1"
  local new_version_id="$2"
  local old_json
  local new_json

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
    rm -f "${old_json}" "${new_json}"
    return 1
  fi

  rm -f "${old_json}" "${new_json}"
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

run_preflight() {
  local -a args=(functions)
  if [[ "${QPI_SKIP_FUNCTION_SCHEMA_CHECK}" == "1" ]]; then
    args+=(--skip-schema-check)
  fi
  eval "$("${script_dir}/preflight.sh" "${args[@]}")"
}

deploy_bundle() {
  local bundle_path
  local bundle_size
  local bundle_sha256
  local version_id
  local description
  local created_json
  local created_version_id
  local -a create_args

  resolve_git_token
  qpi_require_env "GH_TOKEN"
  qpi_require_env "YC_FOLDER_ID"
  qpi_require_env "BOT_VM_HOST"
  qpi_configure_yc_cli

  qpi_timing_init

  qpi_phase_start "preflight"
  run_preflight
  qpi_phase_end

  qpi_phase_start "bundle"
  bundle_path="${QPI_FUNCTION_BUNDLE_PATH:-}"
  if [[ -z "${bundle_path}" ]]; then
    bundle_path="$(build_bundle)"
  fi
  bundle_size="$(wc -c < "${bundle_path}")"
  bundle_sha256="$(sha256sum "${bundle_path}" | awk '{print $1}')"
  qpi_phase_end

  if [[ "${QPI_PREDEPLOY_ONLY}" == "1" ]]; then
    echo "function_name=${function_name}"
    echo "bundle_path=${bundle_path}"
    echo "bundle_size_bytes=${bundle_size}"
    echo "bundle_sha256=${bundle_sha256}"
    qpi_emit_timing_summary "Function Deploy (${service_name})"
    exit 0
  fi

  version_id="$(latest_version_id)"
  description="Direct code-only deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mapfile -d '' -t create_args < <(build_version_create_args "${version_id}")

  qpi_phase_start "publish"
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
  qpi_phase_end

  qpi_phase_start "verify"
  compare_version_configs "${version_id}" "${created_version_id}"
  qpi_phase_end

  echo "function_name=${function_name}"
  echo "bundle_path=${bundle_path}"
  echo "bundle_size_bytes=${bundle_size}"
  echo "bundle_sha256=${bundle_sha256}"
  echo "source_live_version=${version_id}"
  echo "created_version=${created_version_id}"

  qpi_append_step_summary "### Function Deploy Result"
  qpi_append_step_summary ""
  qpi_append_step_summary "- Function: \`${function_name}\`"
  qpi_append_step_summary "- Bundle SHA256: \`${bundle_sha256}\`"
  qpi_append_step_summary "- Created version: \`${created_version_id}\`"
  qpi_append_step_summary ""
  qpi_emit_timing_summary "Function Deploy (${service_name})"
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
