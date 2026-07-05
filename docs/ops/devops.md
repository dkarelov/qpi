# QPI DevOps Knowledge Base

Operational knowledge for infrastructure, CI/CD, deploy wrappers, production
schema operations, logs, and incidents. Read this before editing `.github/**`,
`scripts/deploy/**`, `scripts/common/detect_ci_changes.sh`, `infra/**`, VM or
network configuration, schema rollout paths, or production smoke checks.

Keep this file aligned with code, workflows, and live read-only cloud evidence.
Do not turn volatile instance ids or private IPs into stable prose; resolve them
with the commands below when operating.

## 1. Current Infrastructure Model

Stable names:

- YC folder variable: `YC_FOLDER_ID`; current operator folder is `b1gmeblqlrrvm912n1uq`.
- Marketplace bot instance group: `qpi-bot-ig`, size 1, preemptible, public SSH/health entrypoint.
- PostgreSQL DB VM: private-only, non-preemptible, reached through the bot VM or private runner.
- Private runner VM: `qpi-private-runner`, preemptible, normally stopped, private-only, no public IP.
- Support-bot instance group: `qpi-support-bot-ig`, size 1, private-only, preemptible.
- Support-bot registry: `qpi-support-bot-registry`, repository `support-bot`.
- Cloud Functions: `qpi-daily-report-scrapper`, `qpi-order-tracker`, `qpi-blockchain-checker`.

Read-only lookups:

```bash
terraform -chdir=infra output
terraform -chdir=infra output -raw bot_public_ip

yc compute instance-group list --folder-id "$YC_FOLDER_ID"
yc compute instance get qpi-private-runner --folder-id "$YC_FOLDER_ID" --format json
yc serverless function list --folder-id "$YC_FOLDER_ID"
yc container registry get --name qpi-support-bot-registry --folder-id "$YC_FOLDER_ID"
```

Important runner reality:

- The private runner is configured with `nat = false` in Terraform and should have only a private address in live YC.
- If any local Terraform output still shows `private_runner_public_ip`, verify live YC before trusting it; runner public addresses are not part of the active architecture.
- SSH to the private runner and support-bot VM goes through the marketplace bot VM jump host.
- NAT gateway egress is the supported GitHub connectivity path for the private runner.

## 2. CI/CD Contracts

Workflows:

- `_fast_validation.yml`: reusable GitHub-hosted fast validation; runs uv sync, generated requirements check, Ruff, workflow/shell lint, and fast tests.
- `ci.yml`: PR/manual validation; starts the private runner only for trusted runs that need DB-backed validation.
- `post_merge.yml`: main-branch orchestrator; classifies changed paths, runs fast validation, then routes to `deploy_lane=none|hosted|private`.
- `deploy_runtime.yml`: manual marketplace runtime deploy; `hotfix` can run from GitHub-hosted after a successful post-merge SHA, `release-grade` uses full DB validation and the private runner.
- `deploy_functions.yml`: manual Cloud Function deploy; builds immutable bundles and publishes selected functions.
- `deploy_terraform.yml`: Terraform validate/plan on push, apply only by explicit manual dispatch.
- `support_bot_ci.yml`: support-bot validation/build path for `apps/support-bot/**` plus relevant workflow/script changes.
- `support_bot_deploy.yml`: support-bot build/push/deploy path; docs/tests-only support-bot changes skip image rollout.
- `private_runner_keepalive.yml`: weekly runner registration/dispatch check.

Classification invariants:

- `scripts/common/detect_ci_changes.sh` and `scripts/dev/validation_groups.json` are the shared source of truth for local affected validation and CI deploy selection.
- Indeterminate base/head diffs fail safe: full marketplace DB validation, production schema apply, runtime deploy, and all three Cloud Function deploys.
- Schema-only changes may apply/assert schema without selecting runtime/function rollout; they must remain backward-compatible with already running code.
- Runtime/function rollout is service-scoped. Shared domain/integration changes select only services that consume the changed surface.
- Docs-only and fast-test-only changes must not deploy services on `main`; verify the skip in workflow output after push.

Deploy wrapper invariants:

- Code-only marketplace deploys use `scripts/deploy/runtime.sh` or `scripts/deploy/function.sh <service>`.
- Support-bot runtime deploys use `scripts/deploy/support_bot.sh <image-ref>` or the support-bot deploy workflow.
- Infrastructure mutations are Terraform-only from `infra/`.
- Runtime archives are built from tracked files only; ignored `.env*`, Terraform state/vars, and `.artifacts` must not enter artifacts.
- Cloud Function bundles resolve private Git dependencies into a local wheelhouse; deployed bundles must not contain tokenized GitHub URLs.
- `requirements.txt` is generated from `uv.lock`; never edit it manually.

## 3. Private Runner

Lifecycle:

```bash
YC_FOLDER_ID=<folder-id> \
PRIVATE_RUNNER_INSTANCE_NAME=qpi-private-runner \
PRIVATE_RUNNER_REPO=dkarelov/qpi \
PRIVATE_RUNNER_BOOTSTRAP_TOKEN=<github-runner-admin-token> \
PRIVATE_RUNNER_SSH_PRIVATE_KEY=<private-key> \
scripts/deploy/private_runner.sh ensure-ready

YC_FOLDER_ID=<folder-id> \
PRIVATE_RUNNER_INSTANCE_NAME=qpi-private-runner \
PRIVATE_RUNNER_SSH_PRIVATE_KEY=<private-key> \
scripts/deploy/private_runner.sh status
```

Operational rules:

- Do not hardcode runner ids or IPs in docs/scripts; instances can be recreated.
- `ensure-ready` starts the VM, waits for the GitHub runner to report online, then performs one serial SSH housekeeping pass for autoshutdown and controller refresh.
- The VM powers itself off after about 60 idle minutes without active `Runner.Worker` processes or interactive SSH sessions.
- Runner cloud-init preinstalls `yc`, `uv`, `psqldef`, the GitHub Actions runner agent, and autoshutdown systemd units; workflow installs are defensive fallbacks.
- Keep `.github/actionlint.yaml` aware of the custom `qpi-private` runner label or workflow linting will fail.
- If the runner does not come online, inspect `scripts/deploy/private_runner.sh status`, the GitHub runner record, and the serial console before changing app code.

## 4. Database and Schema Operations

Approved paths:

- Read-only Codex inspection: `qpi-pg-prod` MCP only.
- Manual read-only or repair work: SSH/bastion plus `psql` using secure env handling.
- Production schema apply/assert/cleanup: `scripts/deploy/schema_remote.sh`.
- Schema source of truth: `schema/schema.sql`.

Never use the global `pg-prod` MCP in this repo; it points at a different database.

MCP setup:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh install

BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh smoke

scripts/dev/qpi_pg_mcp_codex.sh install
scripts/dev/qpi_pg_mcp_codex.sh doctor
```

Manual tunnel:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)"
DB_PRIVATE_IP="$(terraform -chdir=infra output -raw db_private_ip)"

ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:"${DB_PRIVATE_IP}":5432 "ubuntu@${BOT_VM_HOST}"
ss -ltnp | rg ':15432\b'
```

Schema commands:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/schema_remote.sh assert-clean

BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/schema_remote.sh apply

BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/schema_remote.sh cleanup-plan
```

Rules:

- Never apply manual DDL directly in production PostgreSQL.
- For production-like drift, run `libs.db.runtime_schema_compat apply` before declarative `schema_cli apply`; use cleanup only after additive compatibility work has backfilled data.
- Marketplace schema tooling intentionally ignores `support_bot.*`; support-bot state lives in the same database but outside the marketplace schema contract.
- Destructive migration smoke must run only against disposable DB names (`scratch|tmp|disposable`).

## 5. Runtime Deploy and Smoke

Marketplace runtime:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
TELEGRAM_BOT_TOKEN=<token> \
TELEGRAM_API_PROXY_URLS='<primary-http-proxy-url>,<secondary-http-proxy-url>' \
TOKEN_CIPHER_KEY=<cipher-key> \
scripts/deploy/runtime.sh deploy
```

Runtime facts:

- `TELEGRAM_UPDATE_MODE=polling` is default.
- Webhook mode is an explicit fallback requiring `TELEGRAM_UPDATE_MODE=webhook` and `BOT_WEBHOOK_SECRET_TOKEN`.
- `TELEGRAM_API_PROXY_URLS` is required in production and must contain HTTP(S) proxy URLs only.
- `TELEGRAM_API_PROXY_URL` is obsolete and rejected by runtime settings.
- Runtime deploys merge explicit env overrides into `/etc/qpi/bot.env`; empty overrides preserve existing values unless `merge_bot_env.py --blank KEY` is used.
- Runtime deploys hard-gate on Telegram `getMe` unless `QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1` is set for an emergency proxy/Telegram outage.

Cloud Functions:

```bash
GH_TOKEN="$(gh auth token)" \
YC_FOLDER_ID=<folder-id> \
YC_TOKEN="$(yc config get token)" \
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/function.sh daily_report_scrapper
```

Function targets are `daily_report_scrapper`, `order_tracker`, and `blockchain_checker`.

Support bot:

```bash
SUPPORT_BOT_VM_SSH_PROXY_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
SUPPORT_BOT_TELEGRAM_BOT_TOKEN=<bot-token> \
SUPPORT_BOT_GROUP_ID=<topic-enabled-supergroup-id> \
SUPPORT_BOT_OWNER_ID=<owner-telegram-id> \
SUPPORT_BOT_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/qpi \
TELEGRAM_API_PROXY_URLS=http://<proxy-host>:<proxy-port> \
scripts/deploy/support_bot.sh cr.yandex/<registry-id>/support-bot:<sha>
```

Support-bot deploy smoke checks verify Redis PING, PostgreSQL schema access from
inside the deployed container, Telegram `getMe`, forum-supergroup `getChat`,
and administrator `getChatMember` with `can_manage_topics`.

Manual support-bot access:

```bash
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)"
yc compute instance-group list-instances --name qpi-support-bot-ig --folder-id "$YC_FOLDER_ID"
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@${BOT_VM_HOST}" \
  -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
```

## 6. Logging, Metrics, and Incidents

Telegram proxy/request metrics:

- `qpi.telegram.proxy.request_attempt`
- `qpi.telegram.proxy.request_exhausted`
- `qpi.telegram.update.received`
- `qpi.telegram.update.delivery_lag_seconds`
- `qpi.telegram.callback.answer_failure`

Monitoring alerts should live in the `qpilka` folder and use the `admin`
notification channel:

- proxy failure rate over 24h, per proxy, with minimum traffic;
- exhausted proxy requests over 10m;
- update delivery lag p95 over 10m;
- callback answer failures over 10m.

Incident heuristics:

- If `notification_outbox` has high `attempt_count`, old `created_at`, delayed `sent_at`, and `last_error='Timed out'`, suspect Telegram egress before business logic.
- A sent outbox row may retain an older `last_error`; interpret it with `status`, `attempt_count`, and `sent_at`.
- Delayed stateful notifications render from JSON captured at enqueue time; stale CTAs are possible.
- If runtime deploy target verification fails, verify the host still belongs to `qpi-bot-ig` in the configured folder.
- If a workflow cannot reach the self-hosted runner, inspect runner status and cloud-init/serial output before touching application code.
- For in-progress GitHub jobs, `gh run watch <run-id> --exit-status` is preferred. `gh run view --job --log` may not stream live logs.

## 7. Terraform Gotchas

- Local Terraform state contains secrets. Do not paste sensitive outputs into docs, issues, or PRs.
- Full workstation `terraform plan` can carry unrelated drift from cloud-init user-data and Yandex Function bundle hashes computed from local builds. For surgical infra work, use targeted plans and review every affected resource.
- `ubuntu_2404_lts_image_id` is pinned to avoid unrelated bot/DB VM replacements when the Ubuntu family image advances.
- In cloud-init templates, only `${` needs escaping as `$${`; a bare `$$` renders literally and bash expands it to the process id.
- The runner has no public address because ephemeral NAT starts hit YC external-address creation limits and public GitHub reachability was unreliable from some YC ranges.
- NAT-gateway egress has been the stable GitHub path for the runner.
