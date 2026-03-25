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

1. `pr-validation`
   - GitHub-hosted fast path on every relevant PR,
   - runs Python lint, fast tests, `actionlint`, and `shellcheck`,
   - uses shared reusable workflow `.github/workflows/_fast_validation.yml`,
   - starts the private runner only for trusted same-repo PRs that need DB-backed validation.

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

5. `post-merge deploy`
   - single `main`-branch orchestrator,
   - runs fast validation once,
   - starts the private runner once,
   - runs DB-backed validation once,
   - selectively deploys runtime and/or functions,
   - cancels stale in-progress runs on newer `main` pushes,
   - powers the runner down afterward.

6. `manual deploy`
   - operator-triggered runtime-only or function-only workflows,
   - keeps targeted rerun/recovery paths separate from the post-merge orchestrator.

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
- The disposable DBs are created with the user from `TEST_DATABASE_URL` as owner; if the URL omits the app user or points at the wrong user, schema apply will fail with `permission denied for schema public`.
- Schema is reapplied before each file/batch.
- Migration smoke is skipped in CI/orchestrated deploy flows unless schema-related files changed.
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

Non-obvious runner details:

- The runner public IP is ephemeral NAT, not a reserved static address. Resolve it through `yc` or `scripts/deploy/private_runner.sh status`; do not hardcode it in scripts/docs.
- GitHub secrets for SSH keys are best stored as base64-encoded private key material. The scripts can decode raw, escaped, or base64 keys, but base64 is the stable GitHub Actions path.
- The runner can auto-update its own GitHub runner binary on first use after a new upstream release. That can cause one short restart before it reports `online` again.

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
- bootstrap scripts configure `yc` from `YC_TOKEN` + `YC_FOLDER_ID` themselves; no preexisting `yc init` profile is required,
- GitHub-hosted validation jobs cache `~/.cache/uv` keyed by Python version and `uv.lock`,
- reusable workflow `.github/workflows/_fast_validation.yml` is the single source of truth for the GitHub-hosted fast-validation sequence,
- `.github/actionlint.yaml` must list the custom `qpi-private` runner label or `actionlint` will fail on every self-hosted workflow reference,
- runner registration is kept warm by the weekly keepalive workflow,
- runner cloud-init preinstalls `yc`, `uv`, and `psqldef` for steady-state self-hosted jobs,
- the post-merge workflow intentionally ignores workflow-only, test-only, and `scripts/dev/**` changes so those validate in PR CI without causing automatic deployments on `main`,
- `scripts/deploy/private_runner.sh ensure-ready` schedules a max-session shutdown failsafe before jobs begin; the end-of-workflow stop path then reschedules a shorter idle shutdown,
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
- prunes old local runtime archives before creating a new one,
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
- prunes old cached bundles before building,
- prints bundle size,
- creates a new version in the intended folder,
- prints the created version id,
- fails on critical config drift after publish.

Current host prerequisite:

- Function bundling requires `zip` on the private runner. It is installed in runner cloud-init and also installed defensively in the deploy-functions workflow.

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
- If a GitHub-hosted bootstrap job says it cannot decode the SSH key, check that the corresponding repo secret is base64-encoded key material rather than a multiline PEM pasted directly.
- If a workflow is waiting behind another run unexpectedly, inspect runner-job concurrency first. Runner-touching concurrency is intentionally serialized; overlapping push-triggered workflows can delay or cancel each other. For debugging, use `workflow_dispatch` one workflow at a time.

DB reset failures:

- Check DB VM SSH reachability from the private runner.
- Confirm `QPI_DB_VM_HOST` and the SSH key are correct.
- Re-run `scripts/dev/reset_remote_test_dbs.sh` directly to isolate reprovision failures from pytest failures.
- If schema apply fails with `permission denied for schema public`, the disposable DB was created with the wrong owner. Verify that `TEST_DATABASE_URL` uses the app DB user and not `postgres` or another admin-only login.

Deploy wrapper failures:

- Runtime deploy failures before upload usually indicate folder/target mismatch, bad SSH, or low disk on the bot VM.
- Function deploy failures after create usually indicate critical config drift or bad `YC_FOLDER_ID` / auth.
- If runtime deploy target verification fails unexpectedly, verify that `BOT_VM_HOST` still belongs to the expected bot instance group in the configured YC folder.
- If function deploy fails during bundling with `zip: command not found`, the runner host drifted from the expected base packages; install `zip` and keep the runner cloud-init/workflow package step aligned.

GitHub Actions warnings:

- `Node 20` deprecation warnings in workflow logs are about GitHub-provided JavaScript actions such as `actions/checkout` and `actions/setup-python`, not about the QPI application stack. They are non-blocking for now but should be cleaned up by moving to action releases that support the newer Node runtime.

Function deploy vs Terraform deploy:

- Use `scripts/deploy/function.sh <service>` when only service/lib/dependency code changed.
- Use `terraform -chdir=infra plan` and, if intended, `terraform apply` for infra mutations.
