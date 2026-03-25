# QPI Phase 7 Live Telegram Baseline

This repository includes the bot runtime, Cloud Functions, plain-SQL domain layer, `psqldef` schema management, and the Telegram seller/buyer/admin UX described in [AGENTS.md](/home/darker/dkarelov/qpi/AGENTS.md).

## Local Setup

```bash
export GH_TOKEN="${GH_TOKEN:-$(gh auth token)}"
scripts/common/setup_private_git_auth.sh
uv sync --frozen --extra dev
cp .env.example .env
```

`.venv` remains the runtime environment path, but it is managed by `uv`. `pyproject.toml` + `uv.lock` are authoritative. `requirements.txt` is generated only for Cloud Function/Terraform compatibility.

## Migration Commands

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
make migrate-plan
make migrate-up
make migrate-down
make migrate-export
```

## Runtime Checks

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
uv run python -m services.bot_api.main --seller-command "/start" --telegram-id 1001 --telegram-username seller

DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
uv run python -m services.bot_api.main --buyer-command "/start" --telegram-id 2001 --telegram-username buyer

DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
TOKEN_CIPHER_KEY=<cipher-key> \
uv run python -m services.daily_report_scrapper.main --once

DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
uv run python -m services.order_tracker.main --once

DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
uv run python -m services.blockchain_checker.main --once
```

## Test Commands

The supported developer entrypoints live under `scripts/dev/`.

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/reset_test_db.sh

scripts/dev/test.sh fast

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh all
```

`integration` and `migration-smoke` are serialized with `/tmp/qpi-test-db.lock`. If a stale session blocks resets:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

If the shared tunnel user cannot create databases, set `TEST_DATABASE_ADMIN_URL` to an admin-capable connection for the reset/cleanup scripts.

## Deploy Commands

Code-only runtime deploy to the bot VM:

```bash
GH_TOKEN="$(gh auth token)" \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TOKEN_CIPHER_KEY=<cipher-key> \
BOT_WEBHOOK_SECRET_TOKEN=<secret> \
scripts/deploy/runtime.sh
```

Code-only Cloud Function deploy:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
scripts/deploy/function.sh daily_report_scrapper
```

Intentional infra changes still go through Terraform:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan
```

## More Detail

See [docs/dev_workflow.md](/home/darker/dkarelov/qpi/docs/dev_workflow.md) for the shared test DB guardrails, deploy wrapper behavior, and troubleshooting notes.
