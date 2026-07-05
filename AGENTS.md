# QPI AGENTS

Per-session operating rules for this repo. Reference knowledge lives in the docs
listed in the Documentation Map below.

Current repo scope:

- qpi marketplace runtime (Python + PostgreSQL),
- companion support-bot runtime (Python + PostgreSQL schema + Redis) under `apps/support-bot`,
- shared Terraform, runner, and deploy conventions.

## Completion Gate

Local implementation and local tests are not enough to call a task complete. The
default completion sequence for any code, schema, infra, workflow, or requirement
change is:

1. finish the implementation,
2. run required local validation,
3. commit the finished work,
4. push the current branch,
5. inspect the triggered GitHub workflows and wait for a terminal state,
6. if workflows fail, keep debugging or report the exact blocker.

Never report success while the pushed commit is red or its required workflows are
still running. Skip commit/push/workflow verification only when the operator
explicitly says to stop before that stage.

## Documentation Map

| File | Content | Read when |
| --- | --- | --- |
| `CONTEXT.md` | Domain vocabulary, relationships, avoided synonyms | naming things, product/domain discussion |
| `docs/product/requirements.md` | Product scope, functional rules per area (seller, buyer, lifecycle, admin/finance, WB, Telegram UX, money/FX) | changing behavior in that feature area |
| `docs/architecture.md` | Component map with decision-bearing annotations | locating ownership of a behavior |
| `docs/ops/devops.md` | Infra state, CI/CD architecture, deploy/runner/Terraform runbooks, operational gotchas | task touches `.github/**`, `scripts/deploy/**`, `infra/**`, runner/VM/network, production schema, logs, incidents |
| `docs/ops/qpi-postgres-mcp.md` | qpi-specific read-only production PostgreSQL MCP architecture and setup | production read-only DB inspection from Codex |
| `docs/dev_workflow.md` | Full test/deploy runbooks: tunnels, DB credentials, reset paths, private runner lifecycle, troubleshooting | running DB-backed validation or manual deploys |
| `apps/support-bot/README.local.md` | Companion support-bot runtime, local validation, deploy, and live verification | task touches `apps/support-bot/**` |
| `docs/backlog.md` | Open items and deliberately deferred improvements | planning follow-up work |
| `docs/adr/` | Recorded architectural decisions | working in an area an ADR touches |
| `docs/agents/issue-tracker.md` | Issues/PRDs in GitHub Issues for `dkarelov/qpi` | triaging or filing issues |
| `docs/agents/triage-labels.md` | Canonical triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) | agent triage |
| `docs/agents/domain.md` | How engineering skills consume domain docs | exploring with domain vocabulary |

Documentation rules:

- Keep docs aligned with actual code and Terraform state: a change that alters product behavior updates `docs/product/requirements.md`, and a change that alters deploy/infra behavior updates `docs/ops/devops.md`, in the same commit.
- Record only current behavior and active decisions; no phase-by-phase history or superseded evolution notes.
- Keep details that affect delivery/operations; omit minor refactors that are obvious from git history and code.

## Technical Constraints and Invariants

- Marketplace services remain Python-only. The companion support-bot runtime is also Python and isolated under `apps/support-bot`.
- Marketplace dependency and environment management are `uv`-based; `.venv` remains the runtime path, but `uv.lock` is the source of truth.
- Support-bot dependency management is nested `uv` under `apps/support-bot/upstream`, with Python 3.14 as the qpi target version.
- `requirements.txt` is generated from `uv.lock` for Cloud Function/Terraform compatibility and is never hand-edited.
- DB access: `psycopg3` + plain SQL only (no ORM).
- For qpi read-only production inspection, use the qpi-specific `qpi-pg-prod` MCP server after it is installed with `scripts/deploy/qpi_pg_mcp.sh` and registered locally with `scripts/dev/qpi_pg_mcp_codex.sh`.
- `qpi-pg-prod` is DBHub launched on demand through SSH stdio on the bot VM jump host. It must not expose HTTP, open a public port, or make PostgreSQL directly reachable from the workstation.
- `qpi-pg-prod` uses the dedicated PostgreSQL role `qpi_mcp_readonly`; DBHub `readonly=true` is only a safety net, not the security boundary.
- Do not use the globally named `pg-prod` MCP for this repo: it is connected to another PostgreSQL database, and its results are invalid for qpi diagnostics, SQL validation, production evidence, and incident investigation.
- For qpi schema changes, production writes, or incident repairs, use the repo-documented `psql`, SSH/bastion, `scripts/deploy/schema_remote.sh`, and CI/private-runner validation paths, not MCP.
- Schema changes only through `schema/schema.sql` + `psqldef`; `schema/schema.sql` is the only schema source of truth.
- Infrastructure mutations are Terraform-only from `infra/`.
- Marketplace bot runtime uses Telegram long polling by default through the configured outbound Telegram proxies. Webhook mode remains available only as an explicit fallback with `TELEGRAM_UPDATE_MODE=webhook` and valid webhook settings.
- Companion support-bot runtime uses long polling and remains private-only.
- Seller and buyer slash-command adapters (`services/bot_api/seller_handlers.py`, `services/bot_api/buyer_handlers.py`, in-chat command dispatch, and `--seller-command` / `--buyer-command`) are supported interfaces, not legacy-only tooling; changes to shared bot flows must update these adapters in the same change whenever the operation remains available by command.
- `SUPPORT_BOT_USERNAME` is an optional marketplace bot runtime env var; when set, seller/buyer screens can build deep links into the support-bot using the public-ref contract in `docs/product/requirements.md`.
- Expected load target: ~100 concurrent users.

## Local Validation Quickstart

```bash
uv sync --frozen --extra dev

# Default local path, no DB needed:
scripts/dev/test.sh fast

# Default path for narrow runtime / UX changes (resolves minimum validation set):
scripts/dev/test.sh affected --base HEAD~1 --head HEAD
scripts/dev/test.sh affected --paths <changed files...>

# Mandatory preflight before any local DB-backed run:
scripts/dev/test.sh doctor

# DB-backed suites (need a real disposable TEST_DATABASE_URL):
scripts/dev/test.sh integration|schema-compat|migration-smoke|all
```

Rules:

- `fast` is the only suite that should normally run on a GitHub-hosted runner; full DB-backed validation belongs on the dedicated private self-hosted runner (`scripts/dev/run_db_tests_on_runner.sh`), not the workstation tunnel.
- For small UI / copy / formatting changes, start with `fast` plus the narrow affected pytest files; do not reach for `integration` or `all` first.
- DB-backed suites require a real disposable test DB URL. Never invent DB credentials: bootstrap the gitignored `.env.test.local` with `scripts/dev/write_test_env.sh --mode tunnel` (derives credentials from local Terraform outputs), or stop at `fast`.
- In GitHub Actions private-runner jobs, `TEST_DATABASE_URL` / `TEST_SCRATCH_DATABASE_URL` come from repo secrets.
- Full runbook — tunnels, credential patterns, auto-tunnel limits, disposable DB model, manifests, stuck-session cleanup — is in `docs/dev_workflow.md`.

## Safety Guardrails

- Never apply manual DDL directly in PostgreSQL.
- Production deployments use the compatibility patch + declarative apply path, not ad-hoc SQL.
- Validate schema changes on the clean path (`apply -> drop -> apply`).
- Destructive migration smoke runs only against disposable DB names (`scratch|tmp|disposable`).
- Do not treat a bot deployment as successful unless schema apply and seller/buyer `/start` smoke checks both pass.

## Security: Accepted Risks and Mandatory Controls

Accepted risks:

- Hot wallet single-key custody.
- Broad SSH ingress (`0.0.0.0/0`) at current stage.
- Manual admin handling remains for finance exceptions.

Mandatory controls:

- Immutable ledger trail for all balance-changing operations.
- Admin audit trail (who/what/when).
- Sensitive chat input cleanup where Telegram permissions allow deletion.
