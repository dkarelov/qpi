#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: remote_rollout_support_bot.sh <release-id> <release-archive> <image-archive>
EOF
}

if [[ $# -ne 3 ]]; then
  usage
  exit 1
fi

release_id="$1"
archive_path="$2"
image_archive_path="$3"
release_dir="/opt/support-bot/releases/${release_id}"
current_link="/opt/support-bot/current"
previous_target="$(readlink -f "${current_link}" || true)"

rollback() {
  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
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

sudo sh -c "docker load -i '${image_archive_path}' > /tmp/support-bot-docker-load.log"

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
