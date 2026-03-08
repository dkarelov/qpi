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
previous_target=""

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

sudo rm -rf "${release_dir}"
sudo install -d -m 0755 "${release_dir}"
sudo tar -xzf "${archive_path}" -C "${release_dir}"
sudo chown -R ubuntu:ubuntu "${release_dir}"

python3 -m venv "${release_dir}/.venv"

ca_bundle="/etc/ssl/certs/ca-certificates.crt"
export SSL_CERT_FILE="${SSL_CERT_FILE:-${ca_bundle}}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${ca_bundle}}"
export PIP_CERT="${PIP_CERT:-${ca_bundle}}"

if grep -q '\${TOKEN_YC_JSON_LOGGER}' "${release_dir}/requirements.txt"; then
  if [[ -z "${TOKEN_YC_JSON_LOGGER:-}" ]]; then
    echo "TOKEN_YC_JSON_LOGGER is required for private dependencies" >&2
    exit 1
  fi
fi

if [[ -n "${TOKEN_YC_JSON_LOGGER:-}" ]]; then
  TOKEN_YC_JSON_LOGGER="${TOKEN_YC_JSON_LOGGER}" \
    "${release_dir}/.venv/bin/pip" install -r "${release_dir}/requirements.txt"
else
  "${release_dir}/.venv/bin/pip" install -r "${release_dir}/requirements.txt"
fi

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
