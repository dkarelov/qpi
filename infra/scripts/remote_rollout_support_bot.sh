#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: remote_rollout_support_bot.sh <release-id> <release-archive> <image-ref>
EOF
}

if [[ $# -ne 3 ]]; then
  usage
  exit 1
fi

release_id="$1"
archive_path="$2"
image_ref="$3"
release_dir="/opt/support-bot/releases/${release_id}"
current_link="/opt/support-bot/current"
previous_target="$(readlink -f "${current_link}" || true)"
registry_host="${image_ref%%/*}"

rollback() {
  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    sudo rm -rf "${current_link}"
    sudo ln -sfn "${previous_target}" "${current_link}"
    sudo chown -h ubuntu:ubuntu "${current_link}" || true
    sudo systemctl restart support-bot.service || true
  fi
  sudo rm -rf "${release_dir}"
}
trap rollback ERR

sudo rm -rf "${release_dir}"
sudo install -d -m 0755 "${release_dir}"
sudo tar -xzf "${archive_path}" -C "${release_dir}"
sudo chown -R ubuntu:ubuntu "${release_dir}"

registry_token="$(
  curl -fsSL \
    -H 'Metadata-Flavor: Google' \
    'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token' | jq -r '.access_token'
)"

if [[ -z "${registry_token}" || "${registry_token}" == "null" ]]; then
  echo "Failed to resolve registry token from metadata service." >&2
  exit 1
fi

printf '%s' "${registry_token}" | sudo docker login --username iam --password-stdin "${registry_host}" 2>&1 | sudo tee /tmp/support-bot-docker-login.log >/dev/null
sudo docker pull "${image_ref}" 2>&1 | sudo tee /tmp/support-bot-docker-pull.log >/dev/null

sudo rm -rf "${current_link}"
sudo ln -sfn "${release_dir}" "${current_link}"
sudo chown -h ubuntu:ubuntu "${current_link}"
sudo systemctl daemon-reload
sudo systemctl restart support-bot.service

for _ in {1..30}; do
  running_services="$(
    sudo docker compose \
      --project-directory "${current_link}" \
      -f "${current_link}/compose.prod.yml" \
      ps --services --status running || true
  )"

  if printf '%s\n' "${running_services}" | grep -qx 'supportbot' \
    && printf '%s\n' "${running_services}" | grep -qx 'mongodb'; then
    exit 0
  fi

  sleep 2
done

sudo docker compose \
  --project-directory "${current_link}" \
  -f "${current_link}/compose.prod.yml" \
  logs --no-color --tail 100 >&2 || true

false
