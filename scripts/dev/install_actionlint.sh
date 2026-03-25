#!/usr/bin/env bash
set -euo pipefail

target_dir="${1:-${HOME}/.local/bin}"

mkdir -p "${target_dir}"

download_url="$(
  python3 - <<'PY'
import json
import urllib.request

url = "https://api.github.com/repos/rhysd/actionlint/releases/latest"
with urllib.request.urlopen(url) as response:
    payload = json.load(response)

for asset in payload["assets"]:
    name = asset.get("name", "")
    if name.endswith("linux_amd64.tar.gz"):
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("Failed to resolve the latest actionlint Linux asset URL.")
PY
)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

curl -fsSL "${download_url}" -o "${tmp_dir}/actionlint.tar.gz"
tar -xzf "${tmp_dir}/actionlint.tar.gz" -C "${tmp_dir}" actionlint
install -m 0755 "${tmp_dir}/actionlint" "${target_dir}/actionlint"
