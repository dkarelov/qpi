# QPI Phase 7 Live Telegram Baseline

This repository includes the marketplace bot runtime, Cloud Functions, plain-SQL domain layer, `psqldef` schema management, the dedicated private CI/deploy runner path, the vendored companion support-bot app under [apps/support-bot](/home/darker/dkarelov/qpi/apps/support-bot), and the operational rules described in [AGENTS.md](/home/darker/dkarelov/qpi/AGENTS.md).

## Current Marketplace Notes

- Buyer order and review tokens now carry both `token_type` and the immutable assignment `task_uuid`.
- Cashback unlock now requires both WB pickup and buyer review confirmation.
- Automatic review verification only succeeds when the token matches the assignment, the review score is `5`, and every required review phrase is present in the review text.
- Failed automatic review verification leaves the purchase in `picked_up_wait_review`; the buyer must either correct the review and resubmit the token or contact support.
- Admins can manually verify a blocked review token from `⚠️ Исключения`, which moves the assignment to `picked_up_wait_unlock` with audit logging.

## Local Setup

```bash
export GH_TOKEN="${GH_TOKEN:-$(gh auth token)}"
scripts/common/setup_private_git_auth.sh
uv sync --frozen --extra dev
cp .env.example .env
```

`.venv` remains the runtime environment path, but it is managed by `uv`. `pyproject.toml` + `uv.lock` are authoritative. `requirements.txt` is generated only for Cloud Function/Terraform compatibility.

Companion support-bot local prerequisites:

- upgrade local NodeSource to `node_24.x`,
- verify `docker compose version`,
- verify `mongosh --version`.

## Local Commands

Fast suites:

```bash
scripts/dev/test.sh fast
```

Shared local test DB path for ad-hoc work:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh schema-compat

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke
```

If stale local sessions block resets:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

Support-bot validation:

```bash
cd apps/support-bot/upstream
npm ci
npm run build
npm test
```

Local Mongo for support-bot:

```bash
docker compose -f apps/support-bot/compose.dev.yml up -d
```

## Private Runner DB Validation

The canonical DB-backed path runs on the dedicated private self-hosted GitHub runner, not over the workstation tunnel.

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@10.131.0.28:5432/qpi_test \
QPI_DB_VM_HOST=10.131.0.28 \
scripts/dev/run_db_tests_on_runner.sh all
```

That script:

- reads the checked-in DB suite manifests,
- recreates disposable test DBs through the DB VM admin path,
- reapplies schema before each file/batch,
- runs ordinary integration, schema-compat, and migration smoke in isolation.

## Direct Deploys

Code-only runtime deploy to the bot VM:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TOKEN_CIPHER_KEY=<cipher-key> \
BOT_WEBHOOK_SECRET_TOKEN=<secret> \
scripts/deploy/runtime.sh
```

Code-only Cloud Function deploy:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
YC_TOKEN="$(yc config get token)" \
scripts/deploy/function.sh daily_report_scrapper
```

Intentional infra changes still go through Terraform:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan
```

Use Terraform only for intentional infra mutations. If only Python/runtime code changed, use the direct deploy wrappers.

Schema-affecting changes must be applied before runtime/function rollout:

```bash
BOT_VM_HOST=<host> \
scripts/deploy/schema_remote.sh apply
```

Post-merge deploy preflight will fail on runtime/function rollout while production schema still drifts from [schema/schema.sql](/home/darker/dkarelov/qpi/schema/schema.sql).

Support-bot deploys use the dedicated support-bot workflow or `scripts/deploy/support_bot.sh` from a runner/private-network context; they do not go through the marketplace runtime wrapper.

## CI / Runner Model

The repository now assumes:

- `fast` runs on GitHub-hosted runners,
- DB-backed validation and code-only deploys run on a dedicated preemptible private runner VM,
- support-bot CI runs on GitHub-hosted runners with Node 24,
- support-bot auto-deploys reuse the same private runner but stay isolated from qpi Python workflows,
- GitHub-hosted bootstrap jobs start that VM on demand,
- a weekly keepalive workflow starts the runner briefly and then powers it down again.

## More Detail

See [docs/dev_workflow.md](/home/darker/dkarelov/qpi/docs/dev_workflow.md) for the private runner lifecycle, DB suite manifests, deploy wrapper behavior, and troubleshooting notes.
