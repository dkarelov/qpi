# Developer Workflow

## Setup

`uv` is required. The project source of truth is `pyproject.toml` + `uv.lock`.

```bash
export GH_TOKEN="${GH_TOKEN:-$(gh auth token)}"
scripts/common/setup_private_git_auth.sh
uv sync --frozen --extra dev
cp .env.example .env
```

Notes:

- `.venv` remains the runtime environment path, but `uv` manages it.
- `requirements.txt` is generated from `uv.lock` and is only kept for Cloud Function/Terraform compatibility.
- If `GH_TOKEN` is unavailable, repo wrappers also accept `TOKEN_YC_JSON_LOGGER` and map it automatically.
- If the shared tunnel user cannot create `qpi_test` and `qpi_test_scratch`, set `TEST_DATABASE_ADMIN_URL` for the reset/cleanup scripts.

## Shared Test DB

The default integration target remains the shared remote `qpi_test` database through the SSH tunnel at `127.0.0.1:15432`.

Recommended flow:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/reset_test_db.sh

scripts/dev/test.sh fast

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke
```

Behavior:

- `fast` runs the non-shared-db suites plus the deterministic Telegram harness.
- `integration` resets `qpi_test` + `qpi_test_scratch`, acquires `/tmp/qpi-test-db.lock`, then runs the main non-migration suite.
- `migration-smoke` uses the same lock and the disposable `qpi_test_scratch` database.
- `all` runs `fast`, `integration`, and `migration-smoke` in order.

Cleanup:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

Use this when resets fail because a previous run left open sessions behind.

## Deploy Commands

Bot VM rollout:

```bash
GH_TOKEN="$(gh auth token)" \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TOKEN_CIPHER_KEY=<cipher-key> \
BOT_WEBHOOK_SECRET_TOKEN=<secret> \
scripts/deploy/runtime.sh
```

The wrapper packages the current tree, uploads it to the VM, applies schema when needed, rebuilds `.venv` with `uv`, verifies health, and runs seller/buyer `/start` smoke checks.

Cloud Functions:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
scripts/deploy/function.sh order_tracker
```

The function wrapper builds a service-scoped bundle with vendored dependencies, then publishes a new version by reusing the live function configuration from Yandex Cloud.

Terraform:

```bash
GH_TOKEN="$(gh auth token)" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan
```

Use Terraform only for intentional infra changes. If only Python/runtime code changed, use the direct deploy wrappers instead.

## Troubleshooting

Private GitHub auth:

- `scripts/common/setup_private_git_auth.sh` is the supported auth bootstrap.
- If `uv sync` or function bundle builds fail on `yc_json_logger`, verify `GH_TOKEN` or `TOKEN_YC_JSON_LOGGER`.

Shared DB lock contention:

- The shared lock lives at `/tmp/qpi-test-db.lock`.
- If an integration run is active, wait for it or clean stale sessions with `scripts/dev/kill-stuck-tests.sh`.

Function deploy vs Terraform deploy:

- Use `scripts/deploy/function.sh <service>` when only service/lib/dependency code changed.
- Use `terraform -chdir=infra plan` and, if intended, `terraform apply` for infra mutations.
