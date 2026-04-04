#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <release_id> <archive_path> [health_port]" >&2
  exit 1
fi

release_id="$1"
archive_path="$2"
health_port="${3:-18080}"

release_dir="/opt/qpi/releases/${release_id}"
current_link="/opt/qpi/current"
shared_venv_root="${QPI_SHARED_VENV_ROOT:-/opt/qpi/shared-venvs}"
previous_target=""
shared_venv_dir=""
shared_venv_state="unknown"

if [[ -L "${current_link}" ]]; then
  previous_target="$(readlink -f "${current_link}" || true)"
fi

rollback() {
  echo "rollout failed, executing rollback" >&2
  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    sudo ln -sfn "${previous_target}" "${current_link}"
    sudo chown -h ubuntu:ubuntu "${current_link}" || true
    sudo systemctl restart qpi-bot.service || true
  fi
}
trap rollback ERR

dependency_fingerprint() {
  python3 - "${release_dir}" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for relative in ("pyproject.toml", "uv.lock", ".python-version"):
    path = root / relative
    if not path.is_file():
        continue
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

ensure_shared_venv() {
  local fingerprint
  local temp_venv_dir

  fingerprint="$(dependency_fingerprint)"
  shared_venv_dir="${shared_venv_root}/${fingerprint}"

  sudo install -d -m 0755 "${shared_venv_root}"
  sudo chown ubuntu:ubuntu "${shared_venv_root}"

  if [[ -x "${shared_venv_dir}/bin/python" ]]; then
    shared_venv_state="reused"
  else
    temp_venv_dir="${shared_venv_root}/.tmp-${fingerprint}-$$"
    rm -rf "${temp_venv_dir}"
    UV_PROJECT_ENVIRONMENT="${temp_venv_dir}" \
      uv sync --frozen --no-dev --project "${release_dir}"
    if [[ -x "${shared_venv_dir}/bin/python" ]]; then
      rm -rf "${temp_venv_dir}"
      shared_venv_state="reused"
    else
      mv "${temp_venv_dir}" "${shared_venv_dir}"
      shared_venv_state="created"
    fi
  fi

  rm -rf "${release_dir}/.venv"
  ln -sfn "${shared_venv_dir}" "${release_dir}/.venv"
  echo "shared venv ${shared_venv_state}: ${shared_venv_dir}"
}

sudo rm -rf "${release_dir}"
sudo install -d -m 0755 "${release_dir}"
sudo tar -xzf "${archive_path}" -C "${release_dir}"
sudo chown -R ubuntu:ubuntu "${release_dir}"

ca_bundle="/etc/ssl/certs/ca-certificates.crt"
export SSL_CERT_FILE="${SSL_CERT_FILE:-${ca_bundle}}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${ca_bundle}}"
export PIP_CERT="${PIP_CERT:-${ca_bundle}}"
export PATH="${HOME}/.local/bin:${PATH}"

if [[ ! -x "${release_dir}/scripts/common/setup_private_git_auth.sh" ]]; then
  echo "private git auth helper missing from release: ${release_dir}/scripts/common/setup_private_git_auth.sh" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

"${release_dir}/scripts/common/setup_private_git_auth.sh"
ensure_shared_venv

sudo ln -sfn "${release_dir}" "${current_link}"
sudo chown -h ubuntu:ubuntu "${current_link}"
sudo systemctl daemon-reload
sudo systemctl restart qpi-bot.service

sleep 5
curl -fsS "http://127.0.0.1:${health_port}/healthz" >/tmp/qpi-bot-health.json
echo "health check ok"
cat /tmp/qpi-bot-health.json

trap - ERR
echo "rollout complete: ${release_id}"
