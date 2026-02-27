#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: with_private_requirements.sh [--] <command> [args...]

Temporarily renders TOKEN_YC_JSON_LOGGER into requirements.txt for the wrapped
command and always restores the placeholder version afterward.
EOF
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 1
fi

if [[ "${1:-}" == "--" ]]; then
  shift
fi

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 1
fi

if [[ -z "${TOKEN_YC_JSON_LOGGER:-}" ]] && command -v gh >/dev/null 2>&1; then
  TOKEN_YC_JSON_LOGGER="$(gh auth token 2>/dev/null || true)"
  export TOKEN_YC_JSON_LOGGER
fi

if [[ -z "${TOKEN_YC_JSON_LOGGER:-}" ]]; then
  echo "TOKEN_YC_JSON_LOGGER is required (or configure 'gh auth token')." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
requirements_path="${repo_root}/requirements.txt"
backup_path="$(mktemp)"

cp "${requirements_path}" "${backup_path}"

restore() {
  cp "${backup_path}" "${requirements_path}"
  rm -f "${backup_path}"
}
trap restore EXIT INT TERM

python - "${requirements_path}" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
token = os.environ["TOKEN_YC_JSON_LOGGER"].strip()
if not token:
    raise SystemExit("TOKEN_YC_JSON_LOGGER is empty")

source = path.read_text(encoding="utf-8")
placeholder = "${TOKEN_YC_JSON_LOGGER}"
if placeholder not in source:
    raise SystemExit(f"Missing placeholder in {path}")

path.write_text(source.replace(placeholder, token), encoding="utf-8")
PY

"$@"
