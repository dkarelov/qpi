# QPI

QPI is a Telegram marketplace where WB sellers fund buyer cashback in USDT. The
repo contains the marketplace bot runtime, scheduled Cloud Functions, plain-SQL
domain layer, PostgreSQL schema tooling, Terraform, CI/deploy wrappers, and the
companion support-bot runtime under [apps/support-bot](apps/support-bot).

Use [AGENTS.md](AGENTS.md) for repo operating rules, [CONTEXT.md](CONTEXT.md)
for domain vocabulary, [docs/product/requirements.md](docs/product/requirements.md)
for product behavior, [docs/architecture.md](docs/architecture.md) for code
ownership, and [docs/dev_workflow.md](docs/dev_workflow.md) for validation and
deploy runbooks.

## Local Setup

```bash
export GH_TOKEN="${GH_TOKEN:-$(gh auth token)}"
scripts/common/setup_private_git_auth.sh
uv sync --frozen --extra dev
cp .env.example .env
```

`.venv` is managed by `uv`. `pyproject.toml` and `uv.lock` are authoritative;
`requirements.txt` is generated from the lockfile only for Cloud
Function/Terraform compatibility.

Companion support-bot work uses the nested uv project:

```bash
cd apps/support-bot/upstream
uv sync --locked
uv run ruff check .
uv run mypy app/config.py app/bot/storage.py app/bot/support_context.py app/bot/support_runtime.py app/bot/support_topics.py app/bot/newsletter.py app/bot/postgres_smoke.py app/bot/telegram_client.py
uv run pytest
```

See [apps/support-bot/README.local.md](apps/support-bot/README.local.md) for
local Redis, environment variables, and deploy details.

## Validation

Default local path, no DB required:

```bash
scripts/dev/test.sh fast
```

Targeted validation for changed files:

```bash
scripts/dev/test.sh affected --base HEAD~1 --head HEAD
scripts/dev/test.sh affected --paths services/bot_api/telegram_runtime.py
```

DB-backed local runs require a real disposable test database. Bootstrap the
gitignored env file from current Terraform outputs before using them:

```bash
scripts/dev/write_test_env.sh --mode tunnel
scripts/dev/test.sh doctor
scripts/dev/test.sh integration
scripts/dev/test.sh schema-compat
scripts/dev/test.sh migration-smoke
```

The full DB-backed gate is the private runner path:

```bash
scripts/dev/run_db_tests_on_runner.sh all
```

## Deploy Entrypoints

Marketplace runtime:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TELEGRAM_API_PROXY_URLS='<primary-http-proxy-url>,<secondary-http-proxy-url>' \
TOKEN_CIPHER_KEY=<cipher-key> \
scripts/deploy/runtime.sh
```

The marketplace runtime defaults to `TELEGRAM_UPDATE_MODE=polling`; webhook mode
is an explicit fallback only.

Cloud Function:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
YC_TOKEN="$(yc config get token)" \
scripts/deploy/function.sh daily_report_scrapper
```

Schema:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/schema_remote.sh apply
```

Support bot:

```bash
scripts/deploy/support_bot.sh cr.yandex/<registry-id>/support-bot:<sha>
```

Intentional infrastructure changes remain Terraform-only:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan
```

## CI / Runner Model

CI uses GitHub-hosted runners for fast validation and support-bot Python checks.
DB-backed marketplace validation, production schema sync, and private-network
rollouts use the on-demand `qpi-private` self-hosted runner. Post-merge deploy
classification lives in `scripts/common/detect_ci_changes.sh` and
`scripts/dev/validation_groups.json`.

## More Detail

See [docs/dev_workflow.md](docs/dev_workflow.md) for local validation, private
runner lifecycle, DB suite manifests, deploy wrapper behavior, and
troubleshooting.
