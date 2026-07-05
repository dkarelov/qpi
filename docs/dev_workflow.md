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
- Companion support-bot work uses the nested Python/uv project plus Docker Compose; see `apps/support-bot/README.local.md` for the local commands.

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
   - starts the private runner only when DB validation, schema sync, or rollout needs it,
   - runs DB-backed validation once when selected,
   - runs production schema apply/assert separately from service rollout,
   - selectively deploys runtime and/or functions,
   - publishes selected Cloud Functions in parallel from prebuilt bundles,
   - lets runtime and function rollout overlap when schema is already clean,
   - cancels stale in-progress runs on newer `main` pushes,
   - powers the runner down afterward.

6. `manual deploy`
   - operator-triggered runtime-only or function-only workflows,
   - keeps targeted rerun/recovery paths separate from the post-merge orchestrator.

7. `support-bot ci`
   - GitHub-hosted Python/uv workflow for `apps/support-bot/**`,
   - runs `uv sync --locked`, Ruff, mypy, and pytest,
   - builds the production support-bot image,
   - also lints repository workflow/shell files.

8. `support-bot deploy`
   - dedicated `main`-branch and manual workflow,
   - classifies support-bot changes before build/deploy,
   - skips image rollout for docs/tests-only changes,
   - resolves registry metadata before image build so private runner startup can overlap validation/build/push,
   - builds the image on GitHub-hosted runners,
   - starts the same private runner,
   - deploys the image artifact into the private-only support-bot instance group,
   - shuts the runner down afterward.

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

Preflight and targeted validation:

```bash
# Preflight local DB-backed prerequisites (env file, tunnel, psql reachability):
scripts/dev/test.sh doctor

# Targeted local validation from changed files:
scripts/dev/test.sh affected --base HEAD~1 --head HEAD

# Targeted local validation from explicit paths:
scripts/dev/test.sh affected --paths services/bot_api/telegram_runtime.py services/bot_api/telegram_notifications.py
```

- `doctor` is the mandatory preflight before local DB-backed validation; it checks `.env.test.local`, the `127.0.0.1:15432` tunnel when relevant, and `psql` reachability.
- `affected` is the default local path for narrow runtime / UX changes: it resolves the minimum validation set from `scripts/dev/validation_groups.json`, which is the source of truth for local targeted validation. Update that manifest when service ownership or test coverage boundaries change.
- `affected` can escalate from a small runtime-only path to full DB validation when the changed set includes validation-orchestration files (`scripts/dev/test.sh`, workflow selectors, deploy/test wrappers, validation manifest). When checking a product/runtime behavior change inside a larger infra refactor, also run a narrowed affected path for the actual code surface so the result is easier to interpret.
- `doctor`, `affected`, and the local DB-backed suite wrappers auto-load the default `.env.test.local` when `TEST_DATABASE_URL` is still unset; `affected` and the local DB-backed suite wrappers also auto-start the default SSH tunnel when the env file is in tunnel mode and includes `QPI_DB_VM_HOST` plus `QPI_DB_VM_SSH_PROXY_HOST`.
- Tunnel auto-start is best-effort only: it works only for tunnel-mode `.env.test.local` files that include that bastion metadata, and it uses `BatchMode=yes` with the default SSH key path. Missing bastion metadata, a non-default key path, or a key that still needs interactive passphrase entry will fail fast and require a manual tunnel.
- `affected` still reprovisions the disposable DBs before DB-backed pytest targets; the speedup comes from a smaller selected test set, not from skipping DB recreation.

Suite cost and disposable DB model:

- `scripts/dev/test.sh all` is an expensive reprovision path: it recreates disposable DBs, reapplies schema, and runs unrelated DB manifests. For small UI / copy / formatting changes, start with `fast` plus the narrow affected pytest files before using `integration` or `all`.
- `integration` and `schema-compat` reset the disposable DB once per manifest run, not once per file; local and private-runner DB runs rely on per-test truncation for isolation after that reset.
- `qpi_test_template` is the reusable clean template DB for disposable test runs; `qpi_test` and `qpi_test_scratch` are disposable clones of it. The reset helpers rebuild the template only when schema / DB-tooling inputs change.
- `TEST_DATABASE_URL` must include the app DB user because the reset scripts recreate disposable DBs with that user as the database owner; otherwise schema apply fails with `permission denied for schema public`.
- Destructive migration smoke must run only against disposable DB names (`scratch|tmp|disposable`).

`TEST_DATABASE_URL` source of truth:

- `scripts/dev/test.sh fast` is the only default local path that does not need a database URL.
- DB-backed paths require a real disposable PostgreSQL target and are expected to fail fast when `TEST_DATABASE_URL` is unset.
- In a normal local shell, `TEST_DATABASE_URL` is intentionally unset until you export it yourself. The repo does not provide or infer DB credentials.
- The supported local bootstrap path is `scripts/dev/write_test_env.sh`, which derives the current app DB credentials from local Terraform outputs and writes a gitignored `.env.test.local`.
- Use one of these concrete patterns:
  - local tunnel: `postgresql://<app-user>:<password>@127.0.0.1:15432/qpi_test`
  - private runner / DB VM: `postgresql://<app-user>:<password>@<db-private-ip>:5432/qpi_test`
- In `--mode tunnel`, the helper also writes `QPI_DB_VM_HOST` and `QPI_DB_VM_SSH_PROXY_HOST=<bot-public-ip>` so the DB reset helper can SSH to the private DB VM through the bot VM.
- In GitHub Actions private-runner jobs, the same values come from repo secrets `TEST_DATABASE_URL` and `TEST_SCRATCH_DATABASE_URL`.
- Do not invent credentials. If you do not have the real app DB user/password in the current environment, run `fast` only or obtain the value from the operator's secure secret source before continuing.
- There is currently no Yandex Lockbox path for DB test credentials in this repo. Local recovery uses Terraform outputs; CI uses GitHub repo secrets.

Recommended local setup:

```bash
scripts/dev/write_test_env.sh --mode tunnel
source .env.test.local

ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa \
  -L 127.0.0.1:15432:"${QPI_DB_VM_HOST}":5432 \
  "ubuntu@${QPI_DB_VM_SSH_PROXY_HOST}"
```

If SSH connects to the bot VM but stalls during key exchange, retry with the explicit algorithms below before debugging credentials or security groups:

```bash
ssh -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 \
  -i ~/.ssh/id_rsa "ubuntu@${QPI_DB_VM_SSH_PROXY_HOST}"

ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 \
  -i ~/.ssh/id_rsa \
  -L 127.0.0.1:15432:"${QPI_DB_VM_HOST}":5432 \
  "ubuntu@${QPI_DB_VM_SSH_PROXY_HOST}"
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

If `QPI_DB_VM_HOST` is set and `TEST_DATABASE_ADMIN_URL` is unset, `scripts/dev/test.sh integration|schema-compat|migration-smoke|all` will automatically use the DB VM SSH reset path. In workstation tunnel mode, that path now supports an SSH proxy through the bot VM via `QPI_DB_VM_SSH_PROXY_HOST`, so you do not need a separate admin DB password in the common operator setup.

Manual production DB diagnostics:

- Prefer the qpi-specific `qpi-pg-prod` MCP for read-only inspection from Codex. Install it with `scripts/deploy/qpi_pg_mcp.sh install`, register it locally with `scripts/dev/qpi_pg_mcp_codex.sh install`, and verify it with `scripts/dev/qpi_pg_mcp_codex.sh doctor`.
- `qpi-pg-prod` runs DBHub through SSH stdio on the bot VM jump host. It does not expose HTTP, does not create a public listener, and uses the dedicated `qpi_mcp_readonly` database role.
- Do not use the global `pg-prod` MCP for this repo; it is connected to a different PostgreSQL database.
- Use `psql`, the SSH tunnel, and `scripts/deploy/schema_remote.sh` for schema verification/apply, writes, and production repairs.
- Do not put live DB passwords in command arguments; use `PGPASSWORD`, a gitignored env file, or load `/etc/qpi/bot.env` on the VM.
- If the workstation tunnel is unstable, run read-only diagnostics on the bot VM from `/opt/qpi/current` using `.venv/bin/python`; the bot VM does not install `psql` by default.
- Prefer serial, bounded SSH/DB probes during incidents; parallel remote probes can hide the real failure behind SSH or tunnel timeouts.
- When remote shell quoting becomes complex, send the script over stdin with `ssh ... bash -s <<'REMOTE'` so SQL quotes survive intact.

See `docs/ops/qpi-postgres-mcp.md` for the MCP architecture, install flow, and smoke checks.

Cleanup when local resets fail because of stale sessions:

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

## Private Runner DB Validation

The canonical full-suite path runs on the private runner:

```bash
scripts/dev/write_test_env.sh --mode private
source .env.test.local
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

- The runner is private-only and should not have a public IP. Resolve the current private address through `yc` or `scripts/deploy/private_runner.sh status`; do not hardcode runner ids or addresses in scripts/docs.
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
- runner cloud-init preinstalls `yc`, `uv`, `psqldef`, and the autoshutdown controller for steady-state self-hosted jobs,
- the post-merge workflow intentionally ignores docs-only (`AGENTS.md`, `docs/**`), workflow-only, test-only, and `scripts/dev/**` changes so those validate in PR CI without causing automatic deployments on `main`,
- `scripts/deploy/private_runner.sh ensure-ready` refreshes the autoshutdown controller, heartbeats it, and schedules a max-session shutdown failsafe before jobs begin,
- if the runner VM is already running, SSH works, the GitHub runner is registered/online, and the local service is active, `ensure-ready` skips runner reinstall/reconfigure/start,
- the end-of-workflow stop path then reschedules a shorter idle shutdown,
- the cleanup path schedules shutdown rather than treating the runner as always-on infrastructure.

## Direct Deploy Commands

Bot VM rollout:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
BOT_VM_HOST=<host> \
TELEGRAM_BOT_TOKEN=<token> \
TELEGRAM_API_PROXY_URLS='<primary-http-proxy-url>,<secondary-http-proxy-url>' \
TOKEN_CIPHER_KEY=<cipher-key> \
scripts/deploy/runtime.sh
```

`TELEGRAM_UPDATE_MODE` defaults to `polling`. Set `TELEGRAM_UPDATE_MODE=webhook` and provide
`BOT_WEBHOOK_SECRET_TOKEN` only for an intentional webhook fallback rollout.

The wrapper now:

- verifies the target host against the expected YC folder / bot instance group,
- verifies Telegram `getMe` through the configured proxy list before rollout unless explicitly bypassed,
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

Schema-affecting marketplace changes must be applied before runtime/function rollout:

```bash
BOT_VM_HOST=<bot-vm-host> \
scripts/deploy/schema_remote.sh apply
```

The post-merge workflow runs schema apply/assert as its own `schema-sync` stage before marketplace predeploy. Schema/tooling-only changes can therefore validate and sync schema without selecting runtime or function rollout, so schema-only changes must be backward-compatible with already-running runtime/function code. If the workflow cannot resolve the base/head diff, `detect_ci_changes` falls back to full marketplace validation plus runtime and all Cloud Function rollouts.

Support-bot deploys:

- `scripts/deploy/support_bot.sh` is the canonical support-bot rollout wrapper.
- The wrapper expects to run where the support-bot instance group's private IP is reachable, which in CI means the private runner.
- For workstation/manual use, set `SUPPORT_BOT_VM_SSH_PROXY_HOST=<qpi-bot-public-ip>` so the wrapper can proxy through the always-on qpi bot VM.
- The support-bot workflow builds and pushes the image in GitHub Actions; the VM pulls the registry image and does not build it locally.
- The support-bot deploy workflow skips image rollout for docs/tests-only changes and starts the private runner as soon as registry metadata is resolved, in parallel with validation/build/push.
- The support-bot release path expects `/opt/support-bot/current` to be a symlink owned by the deploy wrapper, not a pre-created directory.
- Required runtime inputs are `SUPPORT_BOT_TELEGRAM_BOT_TOKEN`, `SUPPORT_BOT_GROUP_ID`, `SUPPORT_BOT_OWNER_ID`, `SUPPORT_BOT_DATABASE_URL` or `DATABASE_URL`, and `TELEGRAM_API_PROXY_URLS`.
- Optional runtime inputs include `SUPPORT_BOT_DEV_IDS`, `SUPPORT_BOT_DB_SCHEMA`, and `SUPPORT_BOT_REDIS_DB`.
- The deploy wrapper validates Redis PING, PostgreSQL schema access, and Telegram `getMe` through the configured proxy before reporting success.
- Old Mongo data, `/open`, orphan-ticket recovery, old ticket ids, and private staff group support are intentionally outside the new runtime.
- Support-bot cloud-init `runcmd` sections that use `pipefail` must execute through `bash -lc`, not default `sh`.

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
- After pushing to `main`, prefer `gh run watch <run-id> --exit-status` to follow the post-merge orchestrator end to end. Treat long `start-private-runner` / `stop-private-runner` stages as normal VM lifecycle time unless they actually fail or time out.
- `gh run view <run-id> --job <job-id> --log` only works after the job completes. While a job is still running, inspect `gh run view <run-id> --json jobs,status,conclusion,url` or keep `gh run watch` open.
- `gh variable` has no `get` command. For repo workflow vars, inspect with `gh variable list` (or `gh api`) and update with `gh variable set`.
- In the post-merge workflow, a line such as `deploy-functions in 0s` means the function deploy job was skipped because no target functions changed.
- `scripts/deploy/runtime.sh` always writes `SUPPORT_BOT_USERNAME=${SUPPORT_BOT_USERNAME:-}` into the runtime overrides file. Keep `SUPPORT_BOT_USERNAME` wired through `post_merge` and `deploy_runtime`, otherwise runtime deploys will erase support deep links from `/etc/qpi/bot.env`.
- When a runtime feature depends on an optional env var, finish the rollout by checking the live `/etc/qpi/bot.env` (or equivalent env source) and one user-visible behavior, not just the workflow result.

Runtime Telegram egress:

- `curl -fsS http://127.0.0.1:18080/healthz` confirms runtime readiness only; it does not prove the bot can call Telegram.
- The canonical Telegram health check is an authenticated Bot API `getMe` call with the runtime bot token. A bare request to `https://api.telegram.org/` is not enough because it does not prove the token-specific API path works.
- `TELEGRAM_API_PROXY_URLS` is a required production comma/newline-separated ordered list of HTTP(S) proxy URLs. SOCKS URLs are rejected intentionally because the Python runtime is not installed with SOCKS proxy support.
- The marketplace runtime uses long polling by default and creates one Telegram `HTTPXRequest` per configured proxy. For each Bot API request it tries proxy 1, proxy 2, proxy 1, proxy 2, proxy 1, proxy 2; transport failures and HTTP 5xx are retried, while semantic Bot API failures such as 400, 401, 403, and 429 are not retried.
- Retrying ambiguous transport failures can duplicate a Telegram operation if Telegram processed the request but the response was lost. Keep Telegram operations idempotent where the Bot API supports it, and inspect Telegram-side state before replaying failed operator actions manually.
- If callbacks or notifications appear silent or delayed, test Telegram API reachability from the bot VM with the same proxy list the runtime uses:

```bash
set -a
source /etc/qpi/bot.env
set +a
python3 - "${TELEGRAM_API_PROXY_URLS}" <<'PY' | while IFS= read -r proxy_url; do
import re
import sys
for raw_item in re.split(r"[,\n]+", sys.argv[1]):
    item = raw_item.strip()
    if item:
        print(item)
PY
  curl -fsS --connect-timeout 5 --max-time 15 \
    --proxy "${proxy_url}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq '.ok, .result.username'
done
```

- If all configured proxies fail, compare direct Telegram access only to separate proxy failure from regional Telegram reachability before debugging bot logic:

```bash
curl -fsS --connect-timeout 5 --max-time 15 \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" >/dev/null
```

- General outbound HTTPS success does not prove Telegram reachability; during incident triage, test `api.telegram.org` itself.
- Runtime deploys hard-gate on `getMe` by default. For an intentional emergency deploy during a Telegram/proxy outage, set `QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1`; local service health and schema checks still remain mandatory.
- In normal polling mode, `getWebhookInfo` should show an empty webhook URL and no growing `pending_update_count`. A non-empty URL means Telegram may still try to POST updates to the VM.
- Telegram proxy metrics are written to Yandex Monitoring with the bot VM service-account IAM token from metadata:
  - `qpi.telegram.proxy.request_attempt`
  - `qpi.telegram.proxy.request_exhausted`
- Telegram update and callback metrics are written to Yandex Monitoring with the same metadata IAM token:
  - `qpi.telegram.update.received`
  - `qpi.telegram.update.delivery_lag_seconds`
  - `qpi.telegram.callback.answer_failure`
- Configure Yandex Monitoring alerts in the `qpilka` folder and attach both to notification channel `admin`:
  - `qpi-telegram-proxy-failure-rate`: 24h window, per proxy, alarm when failures / attempts `> 0.5`, with a minimum sample of 10 attempts per proxy.
  - `qpi-telegram-proxy-request-exhausted`: 10m window, alarm when exhausted requests are `> 0`.
  - `qpi-telegram-update-lag`: 10m window, alarm when p95 update delivery lag is `> 30s`.
  - `qpi-telegram-callback-answer-failure`: 10m window, alarm when callback answer failures are `> 0`.
- Alert query sketch for failure rate:
  - A: `moving_sum(series_sum(["proxy_index","proxy_host"], "qpi.telegram.proxy.request_attempt"{folderId="<folder-id>", service="custom", outcome="failure", proxy_index="*"}), 24h)`
  - B: `moving_sum(series_sum(["proxy_index","proxy_host"], "qpi.telegram.proxy.request_attempt"{folderId="<folder-id>", service="custom", outcome="*", proxy_index="*"}), 24h)`
  - C: `(A / B) * (drop_below(B, 10) / B)`
  - Trigger on C `> 0.5`.
- Alert query sketch for exhausted requests:
  - A: `moving_sum(series_sum("qpi.telegram.proxy.request_exhausted"{folderId="<folder-id>", service="custom"}), 10m)`
  - Trigger on A `> 0`.
- If `notification_outbox` rows show high `attempt_count`, old `created_at`, delayed `sent_at`, and `last_error='Timed out'`, suspect Telegram API egress before investigating business logic.
- A sent row can still retain an older `last_error`; read it together with `status`, `attempt_count`, and `sent_at`.
- Delayed stateful notification payloads can become stale because they are rendered from JSON captured at enqueue time.
- Follow-up engineering work remains: alert on old or high-attempt outbox rows, improve sent/error state clarity, and revalidate delayed stateful CTAs before sending.

DB reset failures:

- Check DB VM SSH reachability from the private runner.
- Confirm `QPI_DB_VM_HOST` and the SSH key are correct.
- Re-run `scripts/dev/reset_remote_test_dbs.sh` directly to isolate reprovision failures from pytest failures.
- If schema apply fails with `permission denied for schema public`, the disposable DB was created with the wrong owner. Verify that `TEST_DATABASE_URL` uses the app DB user and not `postgres` or another admin-only login.

Deploy wrapper failures:

- Runtime deploy failures before upload usually indicate folder/target mismatch, bad SSH, or low disk on the bot VM.
- Function deploy failures after create usually indicate critical config drift or bad `YC_FOLDER_ID` / auth.
- If preflight fails with `Schema drift detected. Run python -m libs.db.schema_cli cleanup-apply first.`, apply the marketplace schema through `scripts/deploy/schema_remote.sh apply` before rerunning the workflow.
- If runtime deploy target verification fails unexpectedly, verify that `BOT_VM_HOST` still belongs to the expected bot instance group in the configured YC folder.
- If function deploy fails during bundling with `zip: command not found`, the runner host drifted from the expected base packages; install `zip` and keep the runner cloud-init/workflow package step aligned.

GitHub Actions warnings:

- Runner Node-runtime deprecation warnings are about GitHub-provided JavaScript actions such as `actions/checkout`, `actions/setup-python`, or artifact actions, not about the QPI Python application stack. Check action major versions before changing app/runtime assumptions.

Function deploy vs Terraform deploy:

- Use `scripts/deploy/function.sh <service>` when only service/lib/dependency code changed.
- Use `terraform -chdir=infra plan` and, if intended, `terraform apply` for infra mutations.
