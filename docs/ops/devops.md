# QPI DevOps Knowledge Base

Operational (non-development) knowledge for this repo: infrastructure state,
CI/CD architecture, deploy/runner/Terraform runbooks, and operational gotchas.

Read this file BEFORE working on: GitHub workflows or deploy scripts
(`.github/**`, `scripts/deploy/**`, `scripts/common/detect_ci_changes.sh`),
Terraform or cloud-init (`infra/**`), the private runner or any VM/network
issue, production schema operations, smoke checks, logs, or incidents.
Regular feature/test development does not need it.

Maintenance: this file is source of truth for the areas above and follows the
Documentation Policy in `AGENTS.md` — keep it aligned with actual code and
Terraform state in the same change that alters behavior.

## 1. Infrastructure State (Current)

YC folder:

- `b1gmeblqlrrvm912n1uq`

Compute and networking:

- Bot runtime: instance group (`qpi-bot-ig`), size 1, preemptible.
- Bot public IP: `158.160.187.114`.
- Support bot runtime: Terraform-defined private-only instance group (`qpi-support-bot-ig`), size 1, preemptible; resolve live IDs with `terraform -chdir=infra output` after apply.
- Support-bot image registry: Terraform-managed Yandex Container Registry (`qpi-support-bot-registry`) with immutable SHA-tagged images under repository `support-bot`.
- DB VM: private-only (`10.131.0.28`), non-preemptible.
- Private runner VM: `qpi-private-runner` (`fv47djh2aqv62pq449mq`), preemptible, on-demand, private IP `10.130.0.23`.
- The private runner VM has NO public IP: it lives on the private subnet, egresses through the VPC NAT gateway, and is reached over SSH only via the bot VM jump host (`PRIVATE_RUNNER_SSH_PROXY_HOST`, same key material for both hops).
- Private subnet: `10.131.0.0/24` with NAT gateway egress.

Serverless functions:

- `qpi-daily-report-scrapper` (`d4ee0tvqv3jutd2kk3ng`) + hourly trigger.
- `qpi-order-tracker` (`d4edjmt28evde0urt9q4`) + 5-minute trigger.
- `qpi-blockchain-checker` is Terraform-managed in `infra/serverless.tf`.
- CF memory profile: `128 MB`.

Resource snapshot IDs (reference):

- Bot IG: `cl17ilrmf3ukgtg14gbe`
- DB instance: `fv4drfqh36622f5lf1vc`
- NAT gateway: `enpkq1bnf0ij8jcjmf7s`
- Private route table: `enpmdt4gs3gav0qd4nce`
- Private subnet: `fl8oled9cdd9u2efqaae`
- Logging group: `e2345psnoc0appog5lil`

Operational note:

- Always check latest values via `terraform -chdir=infra output` before sensitive operations.


## 2. Deploy and CI/CD Invariants

- Code-only deploy entrypoints are `scripts/deploy/runtime.sh`, `scripts/deploy/function.sh`, and `scripts/deploy/support_bot.sh`; broader infra mutations still remain Terraform-only.
- Cloud Function packaging must be service-scoped to avoid unrelated redeploys.
- The private self-hosted runner VM exists for what genuinely needs in-VPC execution: DB-backed validation (direct test-DB access + DB VM admin SSH) and schema apply/migration paths. Everything else — preflight, schema assert-clean, runtime rollout, function publishing — works from GitHub-hosted runners because all production access goes over SSH to the bot VM public IP (schema operations tunnel through the bot VM).
- Post-merge deploys are lane-selected by `detect_ci_changes` (`deploy_lane`): `private` when DB validation, schema apply, or migration is needed (runner boots, consolidated `deploy-private` job runs everything); `hosted` when only rollout/assert work is needed (no runner, `deploy-hosted` on `ubuntu-latest`); `none` for docs/fast-only changes. Indeterminate diffs and `full_validation=true` always route private.
- When a deploy runs on the private runner after DB validation, the network-heavy rollout also happens from the same YC region as the targets; hosted-lane rollouts trade that locality for skipping the runner boot entirely.
- Marketplace schema sync is a separate post-merge stage: `schema/**`, `libs/db/**`, and schema-runner changes can run DB validation plus production schema apply/assert without selecting runtime or Cloud Function rollout by themselves. Schema-only changes must stay backward-compatible with the currently running runtime and Cloud Functions.
- Runtime/function rollout selection is service-scoped: bot-runtime paths select the VM runtime, Cloud Function paths select only their function target, and shared domain/integration paths select only the services that consume them.
- Runtime release archives are built from tracked repository files only; ignored local files such as `.env*`, Terraform state/vars, and `.artifacts` must never enter deploy artifacts.
- Cloud Function bundles must not contain tokenized GitHub URLs; private Git dependencies are resolved during bundling into a local wheelhouse, and bundled requirements install from those local wheels.
- Support-bot images are now published to a dedicated Yandex Container Registry repository and pulled on the VM during rollout instead of being copied as `docker save` archives.

## 3. CI/CD Overview and Gotchas

Workflows:

- `.github/workflows/ci.yml`:
  - PR-focused validation workflow plus manual dispatch,
  - runs fast tests, `actionlint`, and `shellcheck` on GitHub-hosted runners,
  - starts the private runner only for trusted same-repo PRs / manual runs that actually need DB-backed validation, and now overlaps runner boot with fast validation,
  - skips migration smoke unless schema-related files changed,
  - intentionally ignores support-bot-only paths so companion support-bot changes do not trigger qpi DB validation.
- `.github/workflows/post_merge.yml`:
  - single post-merge orchestrator for `main` pushes and manual reruns,
  - runs fast validation once,
  - routes each push to a deploy lane via `deploy_lane`: `private` boots the runner (overlapped with fast validation), `hosted` deploys straight from `ubuntu-latest` with no runner, `none` stops after fast validation,
  - private lane runs everything in ONE consolidated `deploy-private` job on the runner: DB-backed validation (targeted or full), schema apply (or a standalone assert when nothing else would assert), runtime rollout, and Cloud Function publishing as sequential steps — one checkout/bootstrap/uv-sync instead of four sequential jobs with queue gaps,
  - hosted lane (`deploy-hosted`) mirrors the rollout steps only: runtime deploy (internal preflight + schema assert) and function preflight/publish; presentation-only and other no-DB-impact runtime changes land this way in ~2 minutes,
  - builds the runtime tarball and function bundles inside the deploying step from the checked-out SHA (`runtime.sh deploy` and `function.sh` self-build; no GitHub artifact handoff in this workflow),
  - runtime deploy runs `preflight.sh runtime` internally and always asserts schema (`QPI_DEPLOY_SCHEMA_MODE=never` means never *apply*); function preflight keeps the schema assert when no runtime step ran,
  - step order inherently enforces runtime-before-functions when schema was applied (the old `deploy-functions-after-runtime` special case is gone),
  - `workflow_dispatch` supports `full_validation=true` to force the old all-up DB validation path.
- `.github/workflows/deploy_runtime.yml`:
  - manual runtime deploy path with two modes,
  - `auto` resolves to `hotfix` only for SHAs already on `main` with a successful push-event `post_merge` run for that exact SHA,
  - both modes now run an explicit preflight gate and build a single runtime artifact before any rollout step,
  - `hotfix` keeps GitHub-hosted deploy execution after the artifact is built,
  - `release-grade` keeps fast validation plus full DB-backed validation before rollout and now executes rollout from the private runner,
  - `preflight_only=true` runs checks plus artifact build without touching production,
  - the target SHA is checked out directly in the workflow, so operator reruns are no longer limited to `HEAD`.
- `.github/workflows/deploy_functions.yml`:
  - manual function-only deploy path,
  - keeps release-grade DB-backed validation on the private runner,
  - runs a dedicated preflight gate on the private runner before publish,
  - builds immutable function bundles once on GitHub-hosted runners and publishes those exact bundles from the private runner,
  - `preflight_only=true` runs checks plus bundle build without publishing.
- `.github/workflows/support_bot_ci.yml`:
  - support-bot PR/manual workflow,
  - runs `uv sync --locked`, Ruff, mypy, pytest, and production image build,
  - also runs repo workflow/shell lint so support-bot workflow/script changes are validated without triggering qpi DB suites.
- `.github/workflows/support_bot_deploy.yml`:
  - support-bot `main` auto-deploy plus manual dispatch,
  - classifies changed paths before build/deploy; support-bot docs/tests-only changes do not build or roll out a runtime image,
  - resolves registry metadata before image build so private-runner startup can overlap support-bot validation/build/push,
  - builds and pushes the image to Yandex Container Registry on GitHub-hosted runners,
  - runs a dedicated preflight gate on the private runner before rollout,
  - reuses the existing private runner only for private-network deployment into the support-bot instance group,
  - `preflight_only=true` builds/pushes the image and runs checks without touching the VM release symlink.
- `.github/workflows/private_runner_keepalive.yml`:
  - weekly start of the dedicated private runner,
  - validates runner registration / dispatch path.
- `.github/workflows/deploy_terraform.yml`:
  - terraform validate/plan on push,
  - apply only via explicit manual dispatch guard.
- `.github/dependabot.yml`:
  - weekly `github-actions` checks for workflow action references,
  - opens reviewable PRs for action upgrades before platform deprecations become workflow noise.

Private dependency handling:

- `GH_TOKEN` is the canonical auth variable for private GitHub dependencies.
- Existing `TOKEN_YC_JSON_LOGGER` is still accepted and mapped to `GH_TOKEN` by repo wrappers for backward compatibility.
- `scripts/common/setup_private_git_auth.sh` configures git URL rewriting before `uv` operations that need the private dependency.
- `requirements.txt` is generated from `uv.lock` and kept only as a compatibility artifact for Cloud Function/Terraform packaging.

Private runner / workflow gotchas:

- Repo secrets `PRIVATE_RUNNER_SSH_PRIVATE_KEY`, `DB_VM_SSH_PRIVATE_KEY`, and `BOT_VM_SSH_PRIVATE_KEY` should be stored as base64-encoded private key material. The scripts accept raw / escaped / base64 formats, but base64 is the canonical GitHub Actions format because multiline PEM secrets were brittle during rollout.
- Deploy/bootstrap scripts configure `yc` from `YC_TOKEN` + `YC_FOLDER_ID` on every run; do not assume `yc init` or a preexisting profile on GitHub-hosted or self-hosted runners.
- Shared deploy/bootstrap setup now lives in `.github/actions/setup-qpi-deploy`; use it for runtime/function deploy jobs instead of reintroducing per-workflow tool-install snippets.
- `scripts/deploy/preflight.sh` is the shared predeploy gate. Runtime/function/support-bot workflows should fail there before starting rollout rather than discovering SSH/schema/host problems mid-deploy.
- Manual `preflight_only=true` deploy runs are safe rehearsal paths. Runtime/function variants still run validation plus artifact/bundle build, and support-bot still runs build/test plus image push when the registry exists; they only skip the final remote rollout/publish step.
- GitHub-hosted validation jobs cache `~/.cache/uv` keyed by Python version and `uv.lock` to reduce repeated dependency download cost.
- Fast validation is centralized in reusable workflow `.github/workflows/_fast_validation.yml`; keep PR, post-merge, and manual deploy validation behavior aligned there instead of editing each caller separately.
- When sourcing a shared shell helper from `scripts/**`, keep the `# shellcheck source=...` hint repo-relative (for example `scripts/dev/test_db_template_lib.sh`), not workstation-absolute; CI shellcheck runs against the checked-out repo tree and will fail on local absolute paths even when the script itself works.
- `.github/actionlint.yaml` must keep the custom `qpi-private` self-hosted runner label declared or `actionlint` will fail the validation path even when the workflows are otherwise correct.
- In `post_merge`, all private-network work (DB validation, schema, runtime rollout, function publish) now runs as sequential steps of the single `deploy-private` job under the `qpi-private-runner` concurrency group; other workflows (`ci`, `deploy_functions`, `deploy_runtime` release path, `support_bot_deploy`) still scope that group per runner-touching job.
- `.github/workflows/post_merge.yml` now uses workflow-level concurrency on `main` with stale-run cancellation; the VM-baked idle autoshutdown timer powers the runner off after 60 idle minutes, so canceled runs cannot strand the VM (there is no separate max-session shutdown anymore).
- When debugging CI/deploy behavior, prefer `workflow_dispatch` runs one at a time on `main` instead of relying on overlapping push-triggered workflows.
- The private runner self-updates its GitHub runner binary automatically; the first bring-up after a version change can briefly restart the runner before it comes back online.
- Runner cloud-init now preinstalls `yc`, `uv`, `psqldef` (checksum-pinned), the GitHub Actions runner agent tarball, and the autoshutdown script + systemd units at first boot; workflows keep defensive fallback installs, and `install_or_reconfigure_runner` keeps its download-if-missing branch for bare VMs.
- The psqldef fallback install in `setup-qpi-deploy` is checksum-pinned: `PSQLDEF_VERSION` and `PSQLDEF_SHA256` live together in each workflow `env:` block and must be bumped as a pair. The `uv`/`yc` curl-to-sh installers stay unpinned by choice (vendor-maintained installers, preinstalled on the runner VM) — an accepted supply-chain risk.
- Every workflow job now carries `timeout-minutes`; hitting the timeout is the intended failure mode for hung SSH/uv/test steps that previously idled toward GitHub's 6-hour default while holding the single-slot runner concurrency group.
- The autoshutdown idle-check grants a full idle window after every boot (a `last-activity` stamp persisted from a previous session is ignored), and its systemd timer must NOT use `Persistent=true` — a persistent timer fires the idle-check seconds after boot to "catch up", which combined with a stale stamp powered the VM off before SSH could even come up.
- `private_runner.sh ensure-ready` starts the instance, polls the GitHub API for the runner to come online (the baked systemd service starts the agent at boot), then runs one serial SSH housekeeping call (legacy shutdown cancel, autoshutdown heartbeat, content-hash-guarded controller refresh). No background SSH: a backgrounded subshell's SSH proved unreliable on hosted runners and hung ensure-ready to the job timeout. It emits a phase timing table like the deploy scripts.
- If the runner does not report online within `PRIVATE_RUNNER_ONLINE_TIMEOUT_SECONDS` (default 150), `ensure-ready` dumps diagnostics (instance status, GitHub runner record, runner unit status + journal), re-registers the agent when on-disk `.credentials` are missing (recreated VM with a stale GitHub record), and retries the online wait once before failing.
- The private runner now powers itself off locally after 60 minutes without active `Runner.Worker` processes or interactive SSH sessions; workflows no longer SSH back in just to schedule idle shutdown.
- The post-merge orchestrator still skips docs-only (`AGENTS.md`, `docs/**`) and pure fast-test-only changes on `main`, but validation-orchestration changes (`detect_ci_changes`, targeted-validation manifest/scripts, workflow selectors) now trigger post-merge validation without forcing runtime/function deploys.
- `detect_ci_changes` and `scripts/dev/test.sh affected` share the same checked-in validation manifest; keep local targeted validation and CI/post-merge selection aligned there instead of duplicating trigger logic.
- If `detect_ci_changes` cannot resolve the base/head diff for a `main` push, it falls back to full marketplace validation, production schema apply, runtime rollout, and all Cloud Function rollouts. Manual `post_merge` `full_validation=true` reruns only force full DB validation and do not invent deploy targets when the diff is known.
- Runtime-only Telegram copy/render work belongs in `services/bot_api/telegram_notifications.py`; changing that file should stay in the runtime validation/deploy surface. Shared enqueue/outbox changes in `libs/domain/notifications.py` still affect `order_tracker` and therefore still pull the shared DB validation / function-target selection path.
- Validation-orchestration changes can still boot the private runner and run DB-backed validation on `main`; that is intentional because selector changes must be verified end to end against the private-runner path.
- `gh run watch <run-id> --exit-status` is the preferred operator check after a push, but `start-private-runner` can sit in progress for a while during VM boot and runner registration; do not treat that alone as a failure unless the job times out or subsequent status turns red.
- A code push to `main` can still take several extra minutes after local work is finished because `post_merge` waits for the consolidated `deploy-private` job (DB validation, schema, selected rollouts), but warm-runner detection avoids most fixed bootstrap cost when the private runner is already online.
- A push that changes workflow or deploy-orchestration files can trigger extra workflows beyond `post_merge`. In particular, `.github/workflows/deploy_terraform.yml` is itself a watched path for the `Deploy Terraform` push workflow, so workflow edits may require checking two green runs on `main`, not one.
- Support-bot auto-deploy no longer watches `infra/support_bot*.tf`. Apply Terraform first when changing support-bot registry/VM infra, then rerun `support_bot_deploy` manually or wait for the next support-bot app/runtime push.
- If the support-bot container registry has not been created yet, push-triggered `support_bot_deploy` now exits cleanly after the build-image job explains the skip in `${GITHUB_STEP_SUMMARY}`; manual `workflow_dispatch` runs still fail loudly until Terraform creates the registry.
- Support-bot rollout now assumes the VM can pull `cr.yandex/<registry-id>/support-bot:<sha>` with the attached VM service account. If pull fails after Terraform apply, check the registry IAM binding and VM metadata-token access before debugging compose/systemd.
- `gh run view <run-id> --job <job-id> --log` does not stream in-progress job output; for live inspection use `gh run watch` or `gh run view <run-id> --json jobs,status,conclusion,url` and look at step states instead.
- `gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs` currently returns plain text from the blob backend, not a zip archive; if `gh run view --job --log` is sparse, fetch that endpoint directly and grep the text instead of trying to unzip it.
- `gh variable` has no `get` subcommand. Use `gh variable list`, `gh variable set`, or `gh api` when verifying repo-level workflow vars such as `SUPPORT_BOT_USERNAME`.
- In `post_merge`, skipped steps inside `deploy-private` (for example `Deploy runtime` or `Publish functions` showing as skipped) mean that path was intentionally excluded by service/schema classification; it is not an error condition.
- In the current optimized path, a successful `post_merge` run spends most of its time in private-runner boot and the `deploy-private` steps (DB validation, then selected schema/rollout work); fast validation overlaps the runner boot.
- Release-grade marketplace deploy scripts now print phase timing key-values and write a Markdown timing table to `${GITHUB_STEP_SUMMARY}` when available; use those timings before guessing whether runner boot, packaging, upload, schema, or rollout got slower.
- In manual `post_merge` reruns, `full_validation=true` forces the full DB validation path but does not invent deploy targets; runtime/function deploy jobs still follow the resolved change/deploy target set and may remain skipped.
- In `post_merge`, runtime/function artifacts are built inside the `deploy-private` job from the checked-out SHA (no GitHub artifact handoff). The manual `deploy_runtime.yml` path still builds the artifact on a GitHub-hosted job and hands it to the deploy job via upload/download-artifact; when debugging a manual deploy rerun, inspect the artifact-producing job first.
- Runtime deploys merge explicit env overrides into `/etc/qpi/bot.env`. An empty override value now preserves the existing base value instead of blanking it (`merge_bot_env.py --blank KEY` is the intentional-clear escape hatch), and the merge fails fast when required keys (`TELEGRAM_BOT_TOKEN`, `TOKEN_CIPHER_KEY`, `TELEGRAM_API_PROXY_URLS`, `YC_FOLDER_ID`) end up empty. Still pass optional overrides like `SUPPORT_BOT_USERNAME` through workflow env when a deploy should update them; an omitted-or-empty value keeps the current VM setting.
- Bot runtime rollout now reuses a lockfile-keyed shared `.venv` under `/opt/qpi/shared-venvs` when `pyproject.toml` / `uv.lock` are unchanged; code-only deploys still unpack a fresh release and run the same health/smoke checks, but they no longer rebuild dependencies every time.
- The shared-venv deploy optimization helps only after the target lock hash already exists on the VM. The first deploy for a new `uv.lock` / `pyproject.toml` fingerprint still has to build that environment once, so do not expect the first post-change rollout to show the full timing win.
- After fixing workflow/env propagation for an optional runtime feature, verify the live target directly (`/etc/qpi/bot.env`, service health, and one relevant UX path) instead of trusting the workflow green status alone.
- Workflow action references target Node24-ready `actions/checkout@v6` and `actions/setup-python@v6`; keep the private runner on `v2.329.0` or newer for `checkout@v6` compatibility.
- Artifact handoff in deploy workflows is part of the supported CI/CD contract:
  - keep `actions/upload-artifact` / `actions/download-artifact` on Node24-ready majors,
  - current qpi baseline is `upload-artifact@v4` with `download-artifact@v8`,
  - if GitHub starts warning about deprecated runner Node runtimes again, check core action versions before changing any app/runtime assumptions.
- Function bundle publishing requires `zip`; it is installed both in runner cloud-init and defensively in the GitHub-hosted deploy-functions workflow.
- Runtime and function deploy wrappers prune old `.artifacts` outputs with retention knobs so the private runner workspace does not grow without bound.
- GitHub Actions `Node 20` or future runner-runtime deprecation warnings refer to GitHub-provided JavaScript actions such as `actions/checkout`, `actions/setup-python`, or artifact actions, not to the QPI application stack.

Active development rule:

- During the active development phase, completed runtime/code changes must be verified with the relevant repo test/build/lint steps first, then committed and pushed by default unless the operator explicitly says not to push.
- If the operator does not explicitly opt out, treat `commit + push + verification summary` as part of finishing the task, not as optional follow-up.
- Deploy completed changes by default unless the operator explicitly says not to deploy.
- When the expected code diff is small but the default finish path is expensive, call that out before starting the push/deploy stage so the operator can choose between `local verification only` and `full rollout`.
- When a deploy is expected, do not stop at a successful push or workflow trigger: verify the live target state after rollout (service health, active release/image, and at least one relevant smoke check) before considering the task complete.
- If a deployment fails, treat fixing the deployment path as part of completing the task instead of stopping after the failed rollout.


## 4. Runbooks

### Terraform

```bash
uv sync --frozen --extra dev

terraform -chdir=infra init
terraform -chdir=infra fmt
terraform -chdir=infra validate

GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" \
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan

GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" \
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra apply

terraform -chdir=infra output
```

Code-only deploy rule:

- If only marketplace Python/runtime code changed, use `scripts/deploy/runtime.sh` or `scripts/deploy/function.sh <service>` instead of `terraform apply`.
- If only support-bot app/runtime code changed, use the support-bot deploy workflow or `scripts/deploy/support_bot.sh` instead of `terraform apply`.
- The runner deliberately has no public address. Ephemeral NAT made every VM start an external-address creation event counted against the undocumented `vpc.externalAddressesCreation.rate` quota (a burst of starts/recreates exhausted it and blocked the runner for hours), and both ephemeral and reserved public IPs proved unreliable toward `github.com` from some YC ranges (api.github.com worked while github.com was blackholed, so the agent could neither register nor long-poll). NAT-gateway egress is stable toward GitHub.
- `ubuntu_2404_lts_image_id` is pinned in Terraform to avoid unrelated bot/DB VM replacements when the Ubuntu family image advances.

### SSH and DB access

```bash
yc compute instance-group list-instances --name qpi-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -i ~/.ssh/id_rsa ubuntu@158.160.187.114
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@10.131.0.28
```

SSH key-exchange fallback:

```bash
ssh -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 \
  -i ~/.ssh/id_rsa ubuntu@158.160.187.114
```

Support-bot private-only access:

```bash
yc compute instance-group list-instances --name qpi-support-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
```

Manual support-bot deploy via workstation bastion:

```bash
SUPPORT_BOT_VM_SSH_PROXY_HOST=158.160.187.114 \
SUPPORT_BOT_TELEGRAM_BOT_TOKEN=<bot-token> \
SUPPORT_BOT_GROUP_ID=<topic-enabled-supergroup-id> \
SUPPORT_BOT_OWNER_ID=<owner-telegram-id> \
SUPPORT_BOT_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/qpi \
TELEGRAM_API_PROXY_URLS=http://<proxy-host>:<proxy-port> \
scripts/deploy/support_bot.sh cr.yandex/<registry-id>/support-bot:<sha>
```

Read-only production PostgreSQL MCP:

```bash
# Install/refresh DBHub on the bot VM jump host and create the read-only DB role.
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh install

# Verify read-only login and write rejection from the bot VM.
BOT_VM_HOST="$(terraform -chdir=infra output -raw bot_public_ip)" \
scripts/deploy/qpi_pg_mcp.sh smoke

# Register local Codex to launch DBHub through SSH stdio.
scripts/dev/qpi_pg_mcp_codex.sh install
scripts/dev/qpi_pg_mcp_codex.sh doctor
```

`qpi-pg-prod` architecture:

- Codex starts `ssh -T ubuntu@<bot-public-ip> /usr/local/bin/qpi-pg-mcp`.
- SSH carries MCP JSON-RPC over stdin/stdout; there is no DBHub HTTP listener and no systemd daemon.
- `/usr/local/bin/qpi-pg-mcp` starts the pinned local DBHub Docker image with `--transport stdio --config /etc/qpi/dbhub-qpi.toml`.
- DBHub connects from the bot VM to private PostgreSQL through `qpi_mcp_readonly`.
- Remote config lives in `/etc/qpi/qpi-pg-mcp.env`, `/etc/qpi/dbhub-qpi.toml`, and `/etc/qpi/qpi-pg-mcp.image`.

Support-bot live verification:

```bash
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
readlink -f /opt/support-bot/current
systemctl is-active support-bot.service
sudo docker inspect -f '{{.Config.Image}}' current-supportbot-1
sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml \
  exec -T redis redis-cli ping
```

Support-bot deploy smoke checks also verify PostgreSQL schema access from inside the deployed `supportbot` container and Telegram `getMe`, forum-supergroup `getChat`, and administrator `getChatMember` with `can_manage_topics` through `TELEGRAM_API_PROXY_URLS`.
Support-bot release archives contain non-secret runtime files only; `.env` is uploaded separately during rollout and installed on the VM as `0600 ubuntu:ubuntu`.
The support-bot cutover path stops the existing compose stack before switching `/opt/support-bot/current`, starts the new long-polling bot with pending updates preserved, and deletes `/var/lib/support-bot/mongodb` without backup only after Redis, PostgreSQL, proxy `getMe`, forum-supergroup `getChat`, and administrator `getChatMember` checks pass.

Manual Telegram smoke after support-bot cutover:

- contextual `/start` payload,
- first real private message creates a Support Topic,
- topic title renders Telegram account name first, then role/topic/reference context,
- staff text reply reaches the user,
- user media appears in the same Support Topic,
- close/reopen keeps the same topic without creating metadata pins,
- ban ignores further user messages until unbanned.

DB tunnel (default session policy):

```bash
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114
ss -ltnp | rg ':15432\\b'
```

DB tunnel with SSH key-exchange fallback:

```bash
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114
```

Rules:

- Prefer `qpi-pg-prod` MCP for read-only production inspection in Codex sessions.
- Keep the manual tunnel active during active development sessions only when MCP is unavailable or when a psql-only workflow is required.
- Recreate tunnel if listener is missing before DB operations.
- Before any local DB-backed test run, verify the listener first with `ss -ltnp | rg ':15432\\b'`; a missing tunnel can look like a hung pytest/psqldef run instead of failing fast.
- Operator workstation has `psql` available (`PostgreSQL 16.13`); prefer direct `psql` checks over ad-hoc Python probes for schema verification, writes/repairs, and lock/activity checks that need psql-specific behavior.
- If SSH reaches the bot VM but hangs during key exchange, retry with `KexAlgorithms=curve25519-sha256` and `HostKeyAlgorithms=ssh-ed25519` before debugging auth, security groups, or VM state.
- For live DB inspection, avoid putting production DB passwords in command argv; prefer `PGPASSWORD`, a local env file, or remote reads from `/etc/qpi/bot.env`.
- If the workstation tunnel is flaky and `psql` hangs, use the bot VM as the read-only diagnostic host: load `/etc/qpi/bot.env`, run from `/opt/qpi/current`, and use `.venv/bin/python` with `psycopg` because `psql` is not installed on the bot VM by default.
- If a missing local tool would materially improve speed, reliability, or operator clarity, ask the operator to install it instead of defaulting to a slower workaround.
- DB VM security group allows SSH from the private runner security group specifically so `reset_remote_test_dbs.sh` can recreate disposable test DBs through the DB-admin path.
- Support-bot security group allows SSH from the private runner SG and the qpi bot SG; there is no direct public SSH path for the support-bot VM.
- Support-bot security group also keeps TCP/22 open to `0.0.0.0/0` for Yandex instance-group SSH health checks; that does not create direct public access because the VM has no public IP.
- `scripts/deploy/runtime.sh` defaults to `TELEGRAM_UPDATE_MODE=polling`; in that mode it does not require `BOT_WEBHOOK_SECRET_TOKEN` and does not rewrite an existing fallback `WEBHOOK_SECRET_TOKEN`.
- `BOT_WEBHOOK_SECRET_TOKEN` is required only for an intentional `TELEGRAM_UPDATE_MODE=webhook` rollback/fallback rollout; the live bot env file stores it under `WEBHOOK_SECRET_TOKEN`.
- `TELEGRAM_API_PROXY_URLS` is required for production marketplace bot outbound Telegram Bot API calls. It is a comma/newline-separated ordered list of HTTP(S) proxy URLs; SOCKS URLs are intentionally rejected because runtime dependencies do not include SOCKS support. Keep values in runtime env / GitHub Secrets only; do not commit proxy credentials.
- `TELEGRAM_API_PROXY_URL` is no longer supported. Deploy merge deletes the stale key from `/etc/qpi/bot.env`, and runtime settings reject a non-empty legacy value.
- The production Bot API retry order is proxy 1, proxy 2, proxy 1, proxy 2, proxy 1, proxy 2. Transport failures and HTTP 5xx are retried; semantic Telegram errors such as 400, 401, 403, and 429 are not retried. Ambiguous transport retries can duplicate a Telegram operation if Telegram processed the original request but the response was lost.
- Telegram proxy metrics are written to Yandex Monitoring with the bot VM service-account IAM token from metadata:
  - `qpi.telegram.proxy.request_attempt`,
  - `qpi.telegram.proxy.request_exhausted`.
- Telegram update/runtime metrics are written to Yandex Monitoring when `YC_FOLDER_ID` is configured:
  - `qpi.telegram.update.received` with labels `update_type`, `handler`, `outcome`,
  - `qpi.telegram.update.delivery_lag_seconds`,
  - `qpi.telegram.callback.answer_failure`.
- Yandex Monitoring alerts in the `qpilka` folder must be attached to notification channel `admin`:
  - `qpi-telegram-proxy-failure-rate`: 24h window, per proxy, alarm when failures / attempts `> 0.5`, with at least 10 attempts for that proxy.
  - `qpi-telegram-proxy-request-exhausted`: 10m window, alarm when exhausted requests are `> 0`.
  - `qpi-telegram-update-lag`: 10m window, alarm when update delivery lag p95 is `> 30s`.
  - `qpi-telegram-callback-answer-failure`: 10m window, alarm when callback answer failures are `> 0`.
- Runtime deploys hard-gate on Telegram `getMe` by default. `QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1` is the explicit emergency bypass for Telegram/proxy outages; it must not be used to bypass local service health or schema checks.
- The support-bot deploy workflow currently reuses `BOT_VM_SSH_PRIVATE_KEY`; keep that secret valid for both bot and support-bot VM access unless a separate support-bot key is intentionally introduced and verified.

### Schema operations

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m libs.db.runtime_schema_compat apply
uv run python -m libs.db.schema_cli plan
uv run python -m libs.db.schema_cli apply
uv run python -m libs.db.schema_cli cleanup-plan
uv run python -m libs.db.schema_cli cleanup-apply
uv run python -m libs.db.schema_cli assert-clean
uv run python -m libs.db.schema_cli drop
uv run python -m libs.db.schema_cli export
```

Rule:

- Any bot release that starts reading new DB columns must apply schema before the bot process is restarted.
- For production-like legacy drift, run `python -m libs.db.runtime_schema_compat apply` before declarative `schema_cli apply`, and use `schema_cli cleanup-apply` to drop obsolete objects after the additive migration step has backfilled live data.
- Long-lived environments are expected to match `schema/schema.sql` exactly after cleanup; obsolete columns such as `withdrawal_requests.buyer_user_id` and `wb_report_rows.srid` are migration-only artifacts and must not remain in runtime-supported schemas.
- Operator-driven production schema apply remains the SSH-tunnel path to `127.0.0.1:15432`.
- `scripts/deploy/schema_remote.sh` is the canonical production path for `cleanup-plan`, `cleanup-apply`, `apply`, and `assert-clean` against the live DB through the bot-VM SSH bastion.
- Marketplace schema tooling intentionally ignores `support_bot.*` objects because companion support-bot state lives in the same PostgreSQL database under the separate `support_bot` schema; marketplace cleanup must not drop support-bot tables.
- Do not use `qpi-pg-prod` MCP for schema apply, schema cleanup, or manual production repair writes.
- CI/runtime/function deploys must assert that production schema cleanup drift is empty before code rollout; if drift remains, deployment stops until cleanup is applied.
- CI skips production schema apply entirely when no schema-related files changed (`schema/**`, `libs/db/**`, deployment schema runner).

### Runtime smoke checks

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m services.bot_api.main --seller-command "/start" --telegram-id 10001 --telegram-username seller
uv run python -m services.bot_api.main --buyer-command "/start" --telegram-id 10002 --telegram-username buyer
uv run python -m services.daily_report_scrapper.main --once
uv run python -m services.order_tracker.main --once
uv run python -m services.blockchain_checker.main --once
```

Support-bot local validation:

```bash
cd apps/support-bot/upstream
uv sync --locked
uv run ruff check .
uv run mypy app/config.py app/bot/storage.py app/bot/support_context.py app/bot/support_topics.py app/bot/newsletter.py app/bot/telegram_client.py
uv run pytest
docker compose -f ../compose.dev.yml up -d
```

Support-bot live behavior defaults:

- one Support Topic per Telegram Account in the configured topic-enabled support supergroup,
- `SUPPORT_BOT_GROUP_ID` selects the target support supergroup,
- `SUPPORT_BOT_DATABASE_URL` or `DATABASE_URL` points at the existing PostgreSQL cluster,
- `SUPPORT_BOT_DB_SCHEMA` defaults to `support_bot`,
- `SUPPORT_BOT_REDIS_DB` defaults to `7`,
- `TELEGRAM_API_PROXY_URLS` is required and is used for Telegram Bot API verification,
- Redis PING, PostgreSQL schema access, Telegram `getMe`, Telegram `getChat` forum-supergroup validation, and Telegram `getChatMember` administrator `can_manage_topics` validation through the proxy are deploy gates,
- Support Topic titles are the operator metadata surface and use `{name} · {Role topic} · {refs}` so Telegram sidebar truncation preserves the person name,
- runtime handlers use the Support Topic service seam for user delivery, staff replies, topic reopen/recreate, ban, silent, close, and escalation state,
- Telegram `can_delete_messages` is optional; forum service-message cleanup is best effort and must not fail support delivery,
- old Mongo data, `/open`, orphan-ticket recovery, old ticket ids, private staff group support, and old queue preservation are out of scope for the new runtime.

### Logs and incidents

Core correlation fields:

- `telegram_update_id`, `shop_id`, `listing_id`, `assignment_id`, `withdrawal_request_id`, `ledger_entry_id`.

Common checks:

- Telegram update handler failures in bot runtime,
- Telegram update lag and callback answer failures in Yandex Monitoring,
- WB API errors in scrapper/tracker,
- withdrawal backlog and stuck statuses,
- payout send/reject events by request id and tx hash.

Runbook shortcuts:

- Bot outage:
  - `sudo systemctl status qpi-bot.service`
  - `curl -fsS http://127.0.0.1:18080/healthz`
  - remember that `/healthz` only proves runtime readiness; it does not prove outbound Telegram API reachability,
  - verify outbound Telegram API reachability from the bot VM with the authenticated `getMe` Bot API call, loading `/etc/qpi/bot.env` and trying each entry in `TELEGRAM_API_PROXY_URLS`,
  - if one proxy works and another fails, keep the proxy list in place and inspect Yandex Monitoring metric `qpi.telegram.proxy.request_attempt` by `proxy_index`; if every Telegram/proxy path is down and an emergency deploy is still required, set `QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE=1`,
  - do not treat a reachable general HTTPS target such as `google.com` or `ya.ru` as proof that Telegram API egress works,
  - in normal polling mode, verify Telegram `getWebhookInfo` has an empty URL and no growing `pending_update_count`,
  - in explicit webhook fallback mode, verify Telegram `getWebhookInfo` URL/secret alignment.
- Support-bot Support Topic incident:
  - verify `support-bot.service`, `current-supportbot-1`, and `current-redis-1` are running,
  - run Redis PING through compose,
  - inspect `supportbot` logs for delivery or PostgreSQL errors,
  - verify Telegram `getMe`, forum-supergroup `getChat`, and administrator `getChatMember` with `can_manage_topics` through `TELEGRAM_API_PROXY_URLS`,
  - old Mongo data, `/open`, orphan-ticket recovery, old ticket ids, and private staff group routing are not active repair paths.
- CF degradation:
  - inspect logs for `daily_report_scrapper`, `order_tracker`, `blockchain_checker`,
  - verify DB connectivity and runtime env key alignment.
- Notification outbox delay:
  - inspect `notification_outbox` for rows with high `attempt_count`, old `created_at`, delayed `sent_at`, and `last_error = 'Timed out'`,
  - delayed stateful notifications can carry stale CTA payloads because the outbox stores the notification JSON at enqueue time and renders from that payload at send time,
  - successful delivery currently sets `status = 'sent'` but does not clear a previous `last_error`; interpret `last_error` together with `status`, `attempt_count`, and `sent_at`.
- Payout incident:
  - inspect request detail + ledger/audit rows,
  - notify buyer and annotate admin reason,
  - stop further payout actions if ledger consistency is in doubt.

Follow-up engineering work:

- Alert on old or high-attempt `notification_outbox` rows.
- Separate historical delivery errors from current sent state, or clear stale `last_error` when a notification is sent successfully.
- Revalidate current assignment state before sending delayed stateful notification CTAs.


## 5. Hard-Won Operational Gotchas (2026-07 pipeline overhaul)

Yandex Cloud networking:

- `vpc.externalAddressesCreation.rate` is an UNDOCUMENTED folder-level quota: absent from the docs limits page and the console quota screen (which shows only `vpc.externalAddresses.count` / `vpc.externalStaticAddresses.count`). Every start of a VM with ephemeral NAT allocates a new external address and counts against it; failed start attempts appear to count too, so tight retry loops keep the quota saturated. Observed recovery window ~8 hours. Reserving a static address is blocked by the same limiter. Only support can inspect or raise it.
- Some YC public IP ranges are selectively blackholed toward `github.com` while `api.github.com` works, in BOTH directions: the runner agent can neither register (`config.sh` targets github.com) nor long-poll, and inbound SSH from GitHub-hosted runners fails intermittently. Reachability differs per address and per source network — an operator workstation may be unable to reach a YC IP that is perfectly healthy from GitHub or from inside YC. Never conclude "VM is broken" from one vantage point: check the serial console (`yc compute instance get-serial-port-output`) and SSH via the bot VM jump host before touching anything.
- NAT-gateway egress has proven stable toward GitHub; this is why the runner lives on the private subnet with no public IP.

Terraform on this repo (local state):

- A full `terraform plan` from a workstation usually carries unrelated drift: bot/support-bot instance-group `user-data` diffs and `yandex_function` bundle hashes computed from LOCAL builds (the `data "external"` calls in `serverless.tf` rebuild bundles at plan time). Applying that drift would redeploy production functions from a workstation build. For surgical infra ops use `-target=...` and review the plan; watch resource-ordering on targeted applies (for example: detach an address from the instance before destroying the address).
- `terraform.tfstate` is local and contains secrets; the required var `cf_token_cipher_key` can be sourced from state (function env `TOKEN_CIPHER_KEY`) via `TF_VAR_cf_token_cipher_key` for non-interactive applies. The provider needs `YC_TOKEN` (for example `yc iam create-token`).
- `templatefile()` escaping: only `${` needs escaping as `$${`. A bare `$$` NOT followed by `{` renders literally, and bash then expands `$$` to its PID (a `"$$@"` bug produced `curl 21314@`). Before replacing a VM over a cloud-init change, render the template locally (emulate `$${`→`${`, substitute vars) and run `bash -n` on the extracted runcmd script.

Runner VM first boot (cloud-init):

- `runcmd` runs without `HOME`; vendor curl-to-sh installers that use `set -u` (the `yc` installer does) crash on it — the template exports `HOME` explicitly.
- First-boot downloads flake (a TLS handshake to astral.sh failed once); every fetch goes through curl `--retry 5 --retry-all-errors`, and the runner agent + autoshutdown units install BEFORE convenience tools so a tool flake cannot leave the VM runner-less.
- A first boot takes 4-9 minutes (`package_update` + full apt upgrade + downloads) and needrestart bounces sshd mid-way; do not treat slow SSH as a hung VM. `cloud-init status` exits NONZERO when status is `error` — watch loops must not swallow that as "still running".

Runner lifecycle:

- A recreated VM loses on-disk `.credentials` while the GitHub runner record persists; `ensure-ready` re-registers when credentials are missing, but the stale record costs a full online-poll timeout first — deleting the record (`gh api -X DELETE /repos/<repo>/actions/runners/<id>`) after a recreate skips straight to re-registration.
- Do not push changes to `post_merge.yml` (or anything on its `paths` list) while a verification run is in flight: workflow-level concurrency with `cancel-in-progress: true` will cancel it.
