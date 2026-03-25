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

## Execution Split

The supported execution model is:

1. `fast`
   - local workstation or GitHub-hosted runner,
   - no private DB,
   - uses `scripts/dev/test.sh fast`.

2. `db-integration`
   - dedicated private self-hosted runner,
   - fresh `qpi_test` per file,
   - uses `scripts/dev/run_db_tests_on_runner.sh integration`.

3. `schema-compat`
   - dedicated private self-hosted runner,
   - isolated from ordinary DB-backed suites,
   - uses `scripts/dev/run_db_tests_on_runner.sh schema-compat`.

4. `migration-smoke`
   - dedicated private self-hosted runner,
   - fresh `qpi_test_scratch`,
   - uses `scripts/dev/run_db_tests_on_runner.sh migration-smoke`.

5. `deploy`
   - dedicated private self-hosted runner,
   - direct runtime/function wrappers,
   - runner powers down afterward.

## DB Suite Manifests

DB-backed suites are driven by checked-in manifests:

- `tests/db_integration_manifest.txt`
- `tests/schema_compat_manifest.txt`
- `tests/migration_smoke_manifest.txt`

These manifests are the source of truth. `scripts/dev/test.sh fast` validates that all DB-backed tests are declared there before it computes the fast suite.

## Local / Manual Test Paths

Fast tests:

```bash
scripts/dev/test.sh fast
```

Local shared-db path for ad-hoc work:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh schema-compat

TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke
```

The local shared-db path is useful for operator inspection and small manual iterations, but it is not the canonical CI/deploy gate.

Cleanup when local resets fail because of stale sessions:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

## Private Runner DB Validation

The canonical full-suite path runs on the private runner:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@10.131.0.28:5432/qpi_test \
QPI_DB_VM_HOST=10.131.0.28 \
scripts/dev/run_db_tests_on_runner.sh all
```

Behavior:

- `scripts/dev/reset_remote_test_dbs.sh` recreates `qpi_test` and `qpi_test_scratch` through the DB VM admin path.
- Schema is reapplied before each file/batch.
- Ordinary DB integration, schema compatibility, and migration smoke are run separately.
- The runner path does not depend on the workstation SSH tunnel.

## Private Runner Lifecycle

The private runner VM is:

- dedicated,
- preemptible,
- normally powered off,
- started by GitHub-hosted bootstrap jobs,
- used for DB-backed validation and code-only deploys,
- shut down after a short idle window,
- started once weekly by keepalive automation.

Supported lifecycle helper:

```bash
YC_FOLDER_ID=<folder-id> \
PRIVATE_RUNNER_INSTANCE_NAME=<instance-name> \
PRIVATE_RUNNER_REPO=<owner/repo> \
PRIVATE_RUNNER_BOOTSTRAP_TOKEN=<github-runner-admin-token> \
PRIVATE_RUNNER_SSH_PRIVATE_KEY=<private-key> \
scripts/deploy/private_runner.sh ensure-ready
```

Cleanup:

```bash
YC_FOLDER_ID=<folder-id> \
PRIVATE_RUNNER_INSTANCE_NAME=<instance-name> \
PRIVATE_RUNNER_SSH_PRIVATE_KEY=<private-key> \
scripts/deploy/private_runner.sh schedule-stop
```

Implementation notes:

- bootstrap runs on GitHub-hosted runners,
- runner registration is kept warm by the weekly keepalive workflow,
- the cleanup path schedules shutdown rather than treating the runner as always-on infrastructure.

## Direct Deploy Commands

Bot VM rollout:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TOKEN_CIPHER_KEY=<cipher-key> \
BOT_WEBHOOK_SECRET_TOKEN=<secret> \
scripts/deploy/runtime.sh
```

The wrapper now:

- verifies the target host against the expected YC folder / bot instance group,
- checks remote disk/service state before rollout,
- decides schema apply explicitly,
- uploads the release artifact,
- performs health plus seller/buyer `/start` smoke,
- prints a before/after release summary.

Cloud Functions:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
YC_TOKEN="$(yc config get token)" \
scripts/deploy/function.sh order_tracker
```

The function wrapper now:

- builds a service-scoped bundle,
- includes the bundler script in the cache hash,
- prints bundle size,
- creates a new version in the intended folder,
- prints the created version id,
- fails on critical config drift after publish.

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

Private runner bootstrap:

- If a workflow cannot reach the self-hosted runner, inspect `scripts/deploy/private_runner.sh status`.
- Verify `YC_FOLDER_ID`, runner instance name, SSH key, and GitHub runner bootstrap token.
- If the runner disappeared from GitHub after a long idle period, rerun `ensure-ready`; the weekly keepalive should normally prevent that.

DB reset failures:

- Check DB VM SSH reachability from the private runner.
- Confirm `QPI_DB_VM_HOST` and the SSH key are correct.
- Re-run `scripts/dev/reset_remote_test_dbs.sh` directly to isolate reprovision failures from pytest failures.

Deploy wrapper failures:

- Runtime deploy failures before upload usually indicate folder/target mismatch, bad SSH, or low disk on the bot VM.
- Function deploy failures after create usually indicate critical config drift or bad `YC_FOLDER_ID` / auth.

Function deploy vs Terraform deploy:

- Use `scripts/deploy/function.sh <service>` when only service/lib/dependency code changed.
- Use `terraform -chdir=infra plan` and, if intended, `terraform apply` for infra mutations.
