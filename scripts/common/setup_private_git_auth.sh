#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${GH_TOKEN:-}" && -n "${TOKEN_YC_JSON_LOGGER:-}" ]]; then
  GH_TOKEN="${TOKEN_YC_JSON_LOGGER}"
  export GH_TOKEN
fi

if [[ -z "${GH_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
  GH_TOKEN="$(gh auth token 2>/dev/null || true)"
  export GH_TOKEN
fi

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "GH_TOKEN is required for private GitHub dependencies." >&2
  echo "Set GH_TOKEN directly or provide TOKEN_YC_JSON_LOGGER for backward compatibility." >&2
  exit 1
fi

git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
