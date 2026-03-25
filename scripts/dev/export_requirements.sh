#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
output_path="${repo_root}/requirements.txt"
mode="write"

if [[ "${1:-}" == "--check" ]]; then
  mode="check"
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--check]" >&2
  exit 1
fi

tmp_file="$(mktemp)"
trap 'rm -f "${tmp_file}"' EXIT

uv export \
  --project "${repo_root}" \
  --frozen \
  --no-dev \
  --no-editable \
  --no-emit-project \
  --no-header \
  --no-hashes \
  --output-file "${tmp_file}" >/dev/null

{
  echo "# Generated from uv.lock by scripts/dev/export_requirements.sh."
  echo "# Non-authoritative: update pyproject.toml and uv.lock, then re-export."
  cat "${tmp_file}"
} > "${tmp_file}.with-header"

mv "${tmp_file}.with-header" "${tmp_file}"

if [[ "${mode}" == "check" ]]; then
  if ! cmp -s "${tmp_file}" "${output_path}"; then
    echo "requirements.txt is out of date. Run scripts/dev/export_requirements.sh." >&2
    exit 1
  fi
  exit 0
fi

cp "${tmp_file}" "${output_path}"
