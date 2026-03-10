#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <release_id> <archive_path>" >&2
  exit 1
fi

release_id="$1"
archive_path="$2"
stage_dir="/tmp/qpi-schema-${release_id}"
current_link="/opt/qpi/current"
current_python="${current_link}/.venv/bin/python"
runtime_env_path="/etc/qpi/bot.env"
psqldef_path="/tmp/psqldef"
database_url=""

cleanup() {
  sudo rm -rf "${stage_dir}"
}
trap cleanup EXIT

if [[ ! -f "${archive_path}" ]]; then
  echo "archive not found: ${archive_path}" >&2
  exit 1
fi

if [[ ! -x "${current_python}" ]]; then
  echo "current runtime python not found: ${current_python}" >&2
  exit 1
fi

if [[ ! -x "${psqldef_path}" ]]; then
  echo "psqldef binary not found: ${psqldef_path}" >&2
  exit 1
fi

database_url="$(
  sudo awk 'index($0, "DATABASE_URL=") == 1 { sub(/^DATABASE_URL=/, ""); print; exit }' \
    "${runtime_env_path}"
)"
if [[ -z "${database_url}" ]]; then
  echo "DATABASE_URL missing in ${runtime_env_path}" >&2
  exit 1
fi

sudo rm -rf "${stage_dir}"
sudo install -d -m 0755 "${stage_dir}"
sudo tar -xzf "${archive_path}" -C "${stage_dir}"
sudo chown -R ubuntu:ubuntu "${stage_dir}"

export DATABASE_URL="${database_url}"
export PATH="/tmp:${PATH}"
export PYTHONPATH="${stage_dir}"

echo "Applying runtime schema compatibility from ${stage_dir}"
/usr/bin/time -p "${current_python}" -m libs.db.runtime_schema_compat apply

echo "Applying declarative schema from ${stage_dir}"
/usr/bin/time -p "${current_python}" -m libs.db.schema_cli apply
