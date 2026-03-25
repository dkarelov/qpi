#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

require_command actionlint
require_command shellcheck

mapfile -t shell_files < <(
  cd "${repo_root}"
  find scripts infra/scripts -type f -name '*.sh' | sort
)

(
  cd "${repo_root}"
  actionlint
)

if [[ "${#shell_files[@]}" -gt 0 ]]; then
  shellcheck "${shell_files[@]}"
fi
