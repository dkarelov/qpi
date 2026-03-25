# QPI AGENTS

Last updated: 2026-03-26 UTC

## 1. Documentation Policy

`AGENTS.md` is the single project source of truth for:

- current product requirements,
- implemented decisions,
- operating rules,
- deployed infrastructure state,
- runbooks and safeguards.

Documentation rules:

- Keep `AGENTS.md` aligned with actual code and Terraform state.
- Record only current behavior and active decisions.
- Do not keep phase-by-phase history or superseded evolution notes here.
- Keep details that affect delivery/operations; omit minor refactors that are obvious from git history and code.

Glossary for Telegram UX:

- `Объявление` = seller-created buyer-facing offer for one WB product.
- `Покупка` = buyer reservation/work item tied to one announcement.
- `Активно` = user-facing wording for active/open availability or status.

## 2. Product Scope (Current MVP)

Goal:

- Telegram marketplace bot where WB sellers fund buyer cashback in USDT for completing the target purchase flow.

Actors:

- Seller: manages shops and listings, funds collateral, monitors status.
- Buyer: takes tasks, submits verification token, withdraws unlocked cashback.
- Admin: handles withdrawals and finance exceptions.
- One Telegram account can hold multiple capabilities at once (`seller`, `buyer`, `admin`).

In scope:

- Single Telegram bot (`python-telegram-bot`) with role-based UX.
- Russian UX text.
- WB token validation and report-based order lifecycle tracking.
- Ledger in USDT.
- Scheduled CF automation for WB report sync, order tracking, and collateral deposit matching.
- Manual admin override paths for exceptions.

Out of scope (MVP):

- Disputes.
- Advanced custody model (multisig/HSM/KMS-backed wallet signing).
- Full automatic reconciliation for all inbound/outbound wallet flows.

## 3. Implemented System Components

Runtime services:

- `services/bot_api`: always-on webhook bot runtime on VM.
- `services/daily_report_scrapper`: Cloud Function, hourly WB report sync.
- `services/order_tracker`: Cloud Function, 5-minute assignment lifecycle orchestrator.
- `services/blockchain_checker`: Cloud Function, 5-minute seller collateral top-up matcher.
- `services/worker`: placeholder runtime (legacy/no critical ownership).

Shared layers:

- `libs/domain/*`: transactional domain services (plain SQL).
- `libs/domain/seller_workflow.py`: backend seller activation/unpause facade that performs live WB product checks before mutating listing state.
- `libs/integrations/*`: WB/TonAPI/FX clients.
- `libs/config/settings.py`: runtime settings contracts.
- `libs/logging/setup.py`: YC-compatible structured logging.
- `libs/db/*`: pool and schema tooling.
- `scripts/dev/*`: canonical local reset/test/export wrappers.
- `scripts/deploy/runtime.sh`: canonical bot VM rollout entrypoint.
- `scripts/deploy/function.sh`: canonical code-only Cloud Function rollout entrypoint.
- `scripts/deploy/private_runner.sh`: canonical on-demand private runner lifecycle entrypoint for CI/deploy jobs.

Persistence and schema:

- PostgreSQL + `psqldef`.
- `schema/schema.sql` is the only schema source of truth.

## 4. Functional Requirements and Rules

### 4.1 Seller rules

- Seller can create multiple shops.
- Shop names must be unique per seller (case-insensitive).
- Shop name is visible to buyers; seller is warned to use a neutral, understandable title.
- Shop rename regenerates slug/deeplink; seller is warned that old link stops working.
- Shop/listing deletion is soft-delete only.
- Deletion is not blocked by active entities; warning is mandatory before confirmation.
- On confirmed delete:
  - assignment-linked reserved funds go to buyers irreversibly,
  - unassigned collateral returns to seller.
- Listing creation input:
  - `wb_product_id`,
  - cashback in RUB,
  - slot count,
  - search phrase.
- Listing draft creation fetches WB metadata live by `wb_product_id` using the seller's WB token and stores:
  - buyer-visible `display_title`,
  - WB source title,
  - WB subject,
  - WB brand,
  - WB vendor code,
  - WB description,
  - WB photo URL (`c516x688`),
  - WB tech sizes,
  - WB characteristics,
  - buyer price in RUB.
- Buyer price source:
  - primary: derived from `GET https://statistics-api.wildberries.ru/api/v1/supplier/orders` over the last 30 days,
  - fallback: manual seller input when no historical orders exist for the product.
- Seller confirms or edits the buyer-visible title before the draft is saved.
- Seller listing confirmation preview shows the product photo when WB returned one.
- Seller cannot edit an existing announcement after creation; if parameters must change, the seller creates a new announcement and deletes the old one.
- Cashback is converted once to fixed `reward_usdt` at creation.
- Listing collateral requirement: `reward_usdt * slot_count * 1.01`.
- Listing activation/unpause requires:
  - valid WB token,
  - sufficient seller funds,
  - successful live WB metadata read for the stored `wb_product_id`.
- Seller can withdraw only `seller_available`; `seller_collateral` and active collateral holds are never withdrawable.
- Seller can have at most one active withdrawal request at a time.
- Seller can cancel their own withdrawal request while it is still pending admin action and then create a new one.
- Seller withdrawal address must pass TonAPI parse validation for TON mainnet before the request is created.

### 4.2 Buyer rules

- Buyer enters shop by deeplink `shop_<slug>` or by saved shops menu.
- Buyer can reserve slot only on active listings.
- Buyer-facing primary CTA for an active listing is `Купить`.
- Buyer-facing listing screens show buyer-visible title, WB subject, description, photo, sizes, characteristics, cashback in RUB with approximate percent, and `Цена` in RUB.
- Buyer-facing listing screens/cards must not expose WB article (`Артикул WB` / `Артикул ВБ`), WB brand, or WB source title.
- Buyer receives setup token (base64 JSON array):
  - `[search_phrase, wb_product_id, 1, wb_brand_name]`, where `wb_brand_name` is an empty string when unavailable.
- Buyer submits verification token (base64 JSON array):
  - `[order_id, ordered_at]`, where `ordered_at` is an ISO datetime; timezone-bearing values are accepted and normalized to UTC.
- Verification token must be submitted within 4 hours of reservation.
- `order_id` is globally unique (`1 order_id = 1 slot`).
- Buyer can cancel purchase only while the assignment is still `reserved`.
- Buyer cancellation is a distinct terminal lifecycle outcome (`buyer_cancelled`), separate from timeout expiry.
- Validation must happen as early as possible in buyer flows and still be rechecked at final write/transfer time.
- Buyer can have at most one active withdrawal request at a time.
- Buyer can cancel their own withdrawal request while it is still pending admin action and then create a new one.
- Buyer withdrawal address must pass TonAPI parse validation for TON mainnet before the request is created.
- One buyer cannot repeatedly buy the same target item:
  - duplicate reserve attempts are blocked,
  - already-bought item is not treated as a new available task.
- Buyer cannot remove a saved shop while they still have an unfinished purchase in that shop.

### 4.3 Assignment lifecycle rules

In-progress states:

- `reserved`
- `order_verified`
- `picked_up_wait_unlock`

Completed visible state:

- `withdraw_sent`

Terminal/error states:

- `expired_2h`
- `buyer_cancelled`
- `wb_invalid`
- `returned_within_14d`
- `delivery_expired`

Transitions:

- `reserved -> expired_2h` after 4h without valid verification token (legacy status code name retained).
- `reserved -> buyer_cancelled` when the buyer explicitly cancels before submitting a verification token.
- Valid verification token transitions to `order_verified`.
- WB event `Продажа` transitions to `picked_up_wait_unlock` and sets unlock time `pickup + 15d`.
- WB event `Возврат` within unlock window transitions to `returned_within_14d`.
- `order_verified -> delivery_expired` after 60 days without pickup.
- Unlock timer credits buyer balance and transitions assignment to `withdraw_sent` (`Выплачен`).

### 4.4 Admin and finance rules

- Admin operations are Telegram-driven and auditable.
- Withdrawals use one shared requester model for buyers and sellers and require admin decision path:
  - open request,
  - reject with reason, or
  - enter tx hash for a completed transfer.
- Withdrawal completion is single-step:
  - admin enters tx hash only after sending funds,
  - bot verifies the tx hash on-chain against the configured TON USDT payout wallet, requester address, and exact amount,
  - only a verified tx completes the request,
  - failed/missing tx verification leaves the request pending for retry.
- Every new buyer or seller withdrawal request sends an admin push notification with requester role, Telegram identity, amount, and request number.
- Manual deposit is supported for exception handling/bonuses/corrections.
- Manual deposit input supports role aliases:
  - `seller` maps to `seller_available`,
  - `buyer` maps to `buyer_available`.
- External reference for manual deposit is mandatory audit metadata and can be either:
  - free-form reason/comment,
  - tx reference (e.g. `tx:...`).
- `system_payout` balance provisioning remains an accepted implementation shortcut for externally funded credits, but every such top-up must create an immutable audit record in `system_balance_provisions`.

### 4.5 Seller top-up auto-confirmation rules (blockchain checker)

- Matching model: amount suffix on shard address.
- Base amount formula:
  - `base_amount = ceil(request_amount_usdt * 10) / 10`.
- Expected amount formula:
  - `expected_amount = base_amount + suffix / 10000`.
- Suffix space: `001..999`.
- Active invoice uniqueness: one active suffix per `(shard_id, suffix)`.
- Invoice TTL: 24h.
- Match rule:
  - incoming amount must be `>= expected_amount`, on-time.
- Overpayment:
  - full received amount is credited.
- Partial/late payments:
  - route to `manual_review` for admin action.
- MVP shard strategy:
  - one shard address is enabled by default.

### 4.6 WB token and report rules

- Single seller token is used for both:
  - `statistics-api`,
  - `content-api`.
- Seller UX instructs the operator to create a Basic WB token in read-only mode with categories:
  - `Контент`,
  - `Статистика`,
  - `Вопросы и отзывы`.
- WB token initial validation endpoints:
  - `GET https://statistics-api.wildberries.ru/ping`.
  - `GET https://content-api.wildberries.ru/ping`.
- Invalid ping result => token is not persisted.
- Ping rate limit policy: `3 requests / 30s` per process.
- `daily-report-scrapper` invalidates token on WB `401` where message contains:
  - `token expired` => shop token status becomes `expired`,
  - `withdrawn` => shop token status becomes `invalid`,
  - any other `401` auth failure (for example `unauthorized`) => shop token status becomes `invalid`.
- Token invalidation auto-pauses active listings via explicit application transaction (no PG trigger).
- Order tracking matches buyer-submitted `order_id` against WB report identifiers in both forms:
  - exact WB `srid` / stored `wb_srid`,
  - WB `order_uid` and the plain UID segment embedded inside prefixed `srid` values such as `ebu.<uid>.7.0`.

### 4.7 Telegram UX rules (must be preserved)

- Every button press must produce visible feedback:
  - message edit, new reply, or alert.
- Silent no-op callback behavior is forbidden.
- Menus are tree-structured (no flat action panel).
- Callback-driven navigation is immutable/linear:
  - button presses retire the old inline keyboard,
  - the bot sends a new screen message instead of editing the previous one.
- Standard screen layout:
  - title,
  - italic call to action immediately below the title,
  - main content blocks separated by empty lines,
  - optional italic note at the bottom only when it adds non-obvious next steps or issue guidance.
- Button labels include emoji/icon prefix.
- Each role opens with dashboard + section navigation.
- Seller UX:
  - `Объявления`, `Магазины`, `Баланс` are top sections,
  - seller dashboard order counters use the same buckets as buyer dashboard: `ожидают заказа`, `заказаны`, `выкуплены`,
  - shop actions are nested: list -> shop card -> actions.
- Seller announcement list uses numbered, paginated navigation:
  - one page shows up to 10 announcements,
  - each announcement is opened by a number button,
  - action buttons such as edit/pause/delete live inside the announcement card, not in the list.
- Seller announcement card is streamlined:
  - title starts with green/red activity indicator,
  - top section shows only WB article, cashback, search phrase, plan/in-progress, shop link, collateral, and activity status,
  - the rest of the WB data lives inside collapsed `Параметры`, `Описание`, and `Характеристики` sections,
  - if collateral is insufficient, the note explains that balance top-up is required before activation.
- Seller balance screen shows `Свободно для новых объявлений`, `Уже выделено под объявления`, and `В процессе вывода`; `Всего` is not shown there, and activation shortfall is shown only when funds are insufficient.
- Seller balance screen offers:
  - `➕ Пополнить`,
  - `💸 Вывести все доступное`,
  - `✍️ Указать сумму вручную`,
  - `🧾 Транзакции`,
  - `↩️ Назад`.
- If the seller already has an active withdrawal request, new withdrawal actions are hidden and the screen shows that request plus `🚫 Отменить заявку`.
- Seller top-up amount entry screen also includes `Как перевести?`, which opens the same transfer guidance before invoice creation.
- Seller top-up invoice screen:
  - shows the TON USDT address in copy-friendly monospace,
  - shows the exact `USDT` transfer amount in copy-friendly monospace,
  - includes two wallet actions:
    - Telegram Wallet home opener,
    - generic TON jetton transfer link for other wallets,
  - includes `Как перевести?`, which opens a separate guidance screen with Wallet/P2P help links and TON withdrawal steps,
  - keeps the raw address and amount visible as fallback if wallet opening does not work.
- `Транзакции продавца` is a unified paginated seller balance history for both `Пополнение` and `Вывод`, ordered by creation time descending and showing timestamps, statuses, notes/comments, and tx hash when present.
- Transaction/history screens:
  - use representative `Транзакции ...` titles,
  - use `<` / `>` pagination when needed,
  - separate transaction blocks with empty lines,
  - use color indicators for statuses.
- Buyer UX:
  - shops/purchases/balance sections,
  - buyer section title is `Покупки`, not `Задания`,
  - active listing CTA uses `Купить`,
  - shops section uses a numbered, paginated saved-shop list with number buttons, no `Открыть последний магазин`, and no `Открыть магазин по коду` button,
  - each saved shop row shows a red/green circle and `(объявлений: N)`, where green means at least one active buyer-visible listing for that buyer and red means none,
  - buyer shop catalog uses a numbered, paginated listing list; number buttons open the detail card, and the catalog itself does not show direct `Просмотр` / `Купить` buttons,
  - buyer can remove a shop only from their own saved-shop list, and removal is blocked while that shop has an unfinished buyer purchase,
  - if a shop has no other buyer-visible listings but the buyer already has an active purchase there, the shop screen shows a `Покупки` shortcut instead of a dead end,
  - purchase list uses store title (not slug), shows in-progress and paid purchases, and shows fields in order: `Товар`, `Магазин`, `Кэшбэк`, optional `Номер заказа`, `Статус`,
  - purchase status line includes color markers: red for `Ожидает заказа`, yellow for `Заказан`, green for `Выкуплен` / `Выплачен`,
  - dashboard purchase counters are grouped as `ожидают заказа`, `заказаны`, `выкуплены`,
  - buyer dashboard `Баланс` equals withdrawable amount only,
  - buyer balance screen shows only `Доступно для вывода` and `В процессе вывода`,
  - if the buyer already has an active withdrawal request, new withdrawal actions are hidden and the screen shows that request plus a cancel action,
  - buyer withdrawal history is full paginated history with `<` / `>` navigation, timestamps, comments, and tx hash when available,
  - irrelevant actions must be hidden when they cannot be used in the current state (for example withdrawal buttons when withdrawable balance is zero),
  - purchase flow contains explicit submit-token and cancel-purchase actions.
- All user-facing timestamps are rendered in `MSK` (`Europe/Moscow`).
- Admin UX:
  - `Выводы`, `Депозиты`, `Исключения` sections.
- Admin withdrawals section contains pending/actionable requests and processed-history access for both buyers and sellers, with requester role shown in queue and detail views.
- Sensitive inputs (tokens, payloads) are deleted when possible.

### 4.8 Money, precision, and FX rules

- Ledger currency: USDT.
- UI summary format for mixed display:
  - `$USDT (~RUB ₽)`.
- Rounding:
  - USDT summary: 1 decimal,
  - USDT precise operations: 6 decimals,
  - RUB helper: integer rounded.
- FX helper source for `USDT/RUB`:
  - read cached value from PostgreSQL `fx_rates`,
  - if stale (> `FX_RATE_TTL_SECONDS`, default 900), refresh on demand from CoinGecko,
  - refresh is protected by PostgreSQL advisory lock.

## 5. Technical Constraints and Invariants

- Python-only services.
- Dependency and environment management are `uv`-based; `.venv` remains the runtime path, but `uv.lock` is the source of truth.
- `requirements.txt` is generated from `uv.lock` for Cloud Function/Terraform compatibility and is never hand-edited.
- DB access: `psycopg3` + plain SQL only (no ORM).
- Schema changes only through `schema/schema.sql` + `psqldef`.
- Infrastructure mutations are Terraform-only from `infra/`.
- Code-only Cloud Function version publishes are allowed through `scripts/deploy/function.sh`; broader infra mutations still remain Terraform-only.
- Cloud Function packaging must be service-scoped to avoid unrelated redeploys.
- DB-backed CI/deploy execution is designed around a dedicated private self-hosted GitHub runner VM; GitHub-hosted runners only handle fast suites and bootstrap/start-stop orchestration.
- Bot runtime is webhook-based.
- Expected load target: ~100 concurrent users.

## 6. Infrastructure State (Current)

YC folder:

- `b1gmeblqlrrvm912n1uq`

Compute and networking:

- Bot runtime: instance group (`qpi-bot-ig`), size 1, preemptible.
- Bot public IP: `158.160.187.114`.
- DB VM: private-only (`10.131.0.28`), non-preemptible.
- Private runner VM: `qpi-private-runner` (`fv47djh2aqv62pq449mq`), preemptible, on-demand, private IP `10.130.0.23`.
- Private runner public IP is ephemeral NAT and must be resolved dynamically through `yc`, not hardcoded.
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

## 7. Runbooks

### 7.1 Terraform

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

- If only Python/runtime code changed, use `scripts/deploy/runtime.sh` or `scripts/deploy/function.sh <service>` instead of `terraform apply`.
- The runner VM intentionally uses ephemeral NAT instead of a reserved static external IP because the folder hit the external static IP quota during rollout.
- `ubuntu_2404_lts_image_id` is pinned in Terraform to avoid unrelated bot/DB VM replacements when the Ubuntu family image advances.

### 7.2 SSH and DB access

```bash
yc compute instance-group list-instances --name qpi-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -i ~/.ssh/id_rsa ubuntu@158.160.187.114
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@10.131.0.28
```

DB tunnel (default session policy):

```bash
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114
ss -ltnp | rg ':15432\\b'
```

Rules:

- Keep tunnel active during active development sessions.
- Recreate tunnel if listener is missing before DB operations.
- Operator workstation has `psql` available (`PostgreSQL 16.13`); prefer direct `psql` checks over ad-hoc Python probes for DB inspection, schema verification, and lock/activity checks.
- If a missing local tool would materially improve speed, reliability, or operator clarity, ask the operator to install it instead of defaulting to a slower workaround.
- DB VM security group allows SSH from the private runner security group specifically so `reset_remote_test_dbs.sh` can recreate disposable test DBs through the DB-admin path.

### 7.3 Schema operations

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m libs.db.runtime_schema_compat apply
uv run python -m libs.db.schema_cli plan
uv run python -m libs.db.schema_cli apply
uv run python -m libs.db.schema_cli drop
uv run python -m libs.db.schema_cli export
```

Rule:

- Any bot release that starts reading new DB columns must apply schema before the bot process is restarted.
- For production-like legacy drift, run `python -m libs.db.runtime_schema_compat apply` before declarative `schema_cli apply`.
- Operator-driven production schema apply remains the SSH-tunnel path to `127.0.0.1:15432`.
- CI production deploys run `runtime_schema_compat` + `schema_cli apply` on the bot VM itself against the private DB URL from `/etc/qpi/bot.env`, with `psqldef` uploaded to the VM for the run.
- CI skips production schema apply entirely when no schema-related files changed (`schema/**`, `libs/db/**`, deployment schema runner).

### 7.4 Runtime smoke checks

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m services.bot_api.main --seller-command "/start" --telegram-id 10001 --telegram-username seller
uv run python -m services.bot_api.main --buyer-command "/start" --telegram-id 10002 --telegram-username buyer
uv run python -m services.daily_report_scrapper.main --once
uv run python -m services.order_tracker.main --once
uv run python -m services.blockchain_checker.main --once
```

### 7.5 Test runbook

DB URL source of truth:

- `scripts/dev/test.sh fast` does not need `TEST_DATABASE_URL` and is the default local path when no DB credentials are present.
- `scripts/dev/test.sh integration|schema-compat|migration-smoke|all`, `scripts/dev/reset_test_db.sh`, `scripts/dev/reset_remote_test_dbs.sh`, and `scripts/dev/run_db_tests_on_runner.sh` all require a real disposable test DB URL.
- In a plain local shell, `TEST_DATABASE_URL` is normally unset until the operator exports it intentionally. The repo does not contain or infer DB credentials automatically.
- The supported local recovery/bootstrap path is `scripts/dev/write_test_env.sh`, which derives the current app DB credentials from local Terraform outputs and writes a gitignored `.env.test.local`.
- Valid DB-backed patterns are:
  - local tunnel path: `postgresql://<app-user>:<password>@127.0.0.1:15432/qpi_test`,
  - private-runner / DB VM path: `postgresql://<app-user>:<password>@10.131.0.28:5432/qpi_test`.
- In `--mode tunnel`, `scripts/dev/write_test_env.sh` also writes `QPI_DB_VM_HOST` plus `QPI_DB_VM_SSH_PROXY_HOST=<bot-public-ip>` so the DB reset helper can SSH to the private DB VM through the bot VM without requiring a separate DB admin password.
- In GitHub Actions private-runner jobs, `TEST_DATABASE_URL` and `TEST_SCRATCH_DATABASE_URL` come from repo secrets instead of local shell exports.
- Never invent DB credentials. If the concrete value is unavailable in the current environment, stop at `fast` or obtain the real app DB credentials from the operator's secure source before running DB-backed suites.
- There is currently no Yandex Lockbox integration for DB test credentials. The existing recoverable local source is Terraform state / `terraform -chdir=infra output`, and the CI source is GitHub repo secrets.

```bash
uv sync --frozen --extra dev

# Fast local feedback (non-DB suites + deterministic harness):
scripts/dev/test.sh fast

# Write a gitignored local DB test env file from Terraform outputs:
scripts/dev/write_test_env.sh --mode tunnel
source .env.test.local

# Create the local SSH tunnel to the remote DB VM:
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114

# Manual/local shared-db path for ad-hoc work only:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/reset_test_db.sh

# Local ordinary DB integration against the shared test DB:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

# Local schema-compat suite against the shared test DB:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh schema-compat

# Local destructive migration smoke (serialized + reset to qpi_test_scratch):
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke

# Full local ordered suite:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh all

# Canonical private-runner DB suite:
TEST_DATABASE_URL=postgresql://<user>:<password>@10.131.0.28:5432/qpi_test \
QPI_DB_VM_HOST=10.131.0.28 \
scripts/dev/run_db_tests_on_runner.sh all

# Cleanup stale shared-db sessions when resets fail:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

Rules:

- `fast` is the only suite that should normally run on a GitHub-hosted runner.
- Full DB-backed validation should run on the dedicated private self-hosted runner, not over the workstation tunnel.
- `qpi_test` and `qpi_test_scratch` are disposable and must be recreated before DB-backed runs.
- When `QPI_DB_VM_HOST` is set and `TEST_DATABASE_ADMIN_URL` is unset, `scripts/dev/test.sh integration|schema-compat|migration-smoke|all` automatically uses the DB VM SSH reset path instead of requiring a separate local admin DB password.
- From a workstation, that DB VM SSH reset path requires either direct network reachability to `10.131.0.28` or an SSH proxy host. The supported tunnel-mode bootstrap now writes `QPI_DB_VM_SSH_PROXY_HOST` automatically so the reset helper can hop through the bot VM public IP.
- `scripts/dev/reset_remote_test_dbs.sh` is the canonical full-suite reprovision path from the private runner to the DB VM admin path.
- `tests/db_integration_manifest.txt`, `tests/schema_compat_manifest.txt`, and `tests/migration_smoke_manifest.txt` are the source of truth for DB-backed test grouping.
- `TEST_DATABASE_URL` must include the app DB user because the reset scripts recreate disposable DBs with that user as the database owner; otherwise schema apply fails with `permission denied for schema public`.

Safety guardrails:

- Never apply manual DDL directly in PostgreSQL.
- Production deployments should use the compatibility patch + declarative apply path, not ad-hoc SQL.
- Validate schema changes on clean path (`apply -> drop -> apply`).
- Destructive migration smoke must run only against disposable DB names (`scratch|tmp|disposable`).
- Do not treat a bot deployment as successful unless schema apply and seller/buyer `/start` smoke checks both pass.

### 7.6 Logs and incidents

Core correlation fields:

- `telegram_update_id`, `shop_id`, `listing_id`, `assignment_id`, `withdrawal_request_id`, `ledger_entry_id`.

Common checks:

- webhook handler failures in bot runtime,
- WB API errors in scrapper/tracker,
- withdrawal backlog and stuck statuses,
- payout send/reject events by request id and tx hash.

Runbook shortcuts:

- Bot outage:
  - `sudo systemctl status qpi-bot.service`
  - `curl -fsS http://127.0.0.1:18080/healthz`
  - verify Telegram `getWebhookInfo` URL/secret alignment.
- CF degradation:
  - inspect logs for `daily_report_scrapper`, `order_tracker`, `blockchain_checker`,
  - verify DB connectivity and runtime env key alignment.
- Payout incident:
  - inspect request detail + ledger/audit rows,
  - notify buyer and annotate admin reason,
  - stop further payout actions if ledger consistency is in doubt.

## 8. CI/CD Overview

Workflows:

- `.github/workflows/ci.yml`:
  - PR-focused validation workflow plus manual dispatch,
  - runs fast tests, `actionlint`, and `shellcheck` on GitHub-hosted runners,
  - starts the private runner only for trusted same-repo PRs / manual runs that actually need DB-backed validation,
  - skips migration smoke unless schema-related files changed.
- `.github/workflows/post_merge.yml`:
  - single post-merge orchestrator for `main` pushes and manual full reruns,
  - runs fast validation once,
  - starts the private runner once,
  - runs DB-backed validation once,
  - selectively deploys runtime and/or Cloud Functions based on changed files,
  - schedules runner shutdown afterward.
- `.github/workflows/deploy_runtime.yml`:
  - manual runtime-only deploy path,
  - keeps the direct runtime rollout wrapper available for operator-triggered reruns/recovery.
- `.github/workflows/deploy_functions.yml`:
  - manual function-only deploy path,
  - keeps direct function publishes available for operator-triggered reruns/recovery.
- `.github/workflows/private_runner_keepalive.yml`:
  - weekly start of the dedicated private runner,
  - validates runner registration / dispatch path,
  - schedules shutdown afterward.
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
- GitHub-hosted validation jobs cache `~/.cache/uv` keyed by Python version and `uv.lock` to reduce repeated dependency download cost.
- Fast validation is centralized in reusable workflow `.github/workflows/_fast_validation.yml`; keep PR, post-merge, and manual deploy validation behavior aligned there instead of editing each caller separately.
- `.github/actionlint.yaml` must keep the custom `qpi-private` self-hosted runner label declared or `actionlint` will fail the validation path even when the workflows are otherwise correct.
- Runner-touching concurrency is scoped to runner jobs, not whole workflows. Whole-workflow concurrency caused unrelated workflows to cancel each other during rollout.
- `.github/workflows/post_merge.yml` now uses workflow-level concurrency on `main` with stale-run cancellation; `private_runner.sh ensure-ready` sets a max-session shutdown failsafe so canceled runs do not strand the runner VM indefinitely.
- When debugging CI/deploy behavior, prefer `workflow_dispatch` runs one at a time on `main` instead of relying on overlapping push-triggered workflows.
- The private runner self-updates its GitHub runner binary automatically; the first bring-up after a version change can briefly restart the runner before it comes back online.
- Runner cloud-init now preinstalls `yc`, `uv`, and `psqldef`; workflows still keep defensive fallback installs until the runner VM is reprovisioned with the updated image bootstrap.
- The post-merge orchestrator intentionally watches deploy-relevant code/deploy-wrapper paths only; workflow-only, test-only, and `scripts/dev/**` changes validate in PR CI but do not auto-deploy on `main`.
- Workflow action references target Node24-ready `actions/checkout@v6` and `actions/setup-python@v6`; keep the private runner on `v2.329.0` or newer for `checkout@v6` compatibility.
- Function bundle publishing requires `zip` on the private runner. It is installed both in runner cloud-init and defensively in the deploy-functions workflow.
- Runtime and function deploy wrappers prune old `.artifacts` outputs with retention knobs so the private runner workspace does not grow without bound.
- GitHub Actions `Node 20` deprecation warnings refer to GitHub-provided JavaScript actions such as `actions/checkout` / `actions/setup-python`, not to the QPI application stack.

Active development rule:

- During the active development phase, completed runtime/code changes should be committed, pushed, and deployed by default unless the operator explicitly says not to deploy.
- If a deployment fails, treat fixing the deployment path as part of completing the task instead of stopping after the failed rollout.

## 9. Security and Accepted MVP Risks

Accepted risks:

- Hot wallet single-key custody.
- Broad SSH ingress (`0.0.0.0/0`) at current stage.
- Manual admin handling remains for finance exceptions.

Mandatory controls:

- Immutable ledger trail for all balance-changing operations.
- Admin audit trail (who/what/when).
- Sensitive chat input cleanup where Telegram permissions allow deletion.

## 10. Open Items

- Final production payout broadcasting integration and key lifecycle policy.
- Tighten SSH ingress to operator CIDRs.
- Optional migration from self-signed IP TLS to domain-managed trusted TLS.
- Payload integrity/signature scheme for extension tokens (post-MVP).
- Post-MVP WB correction operation semantics (`Коррекция продаж`, `Коррекция возвратов`).
- Optional extraction of CF services into separate repositories.
- Terraform remote backend strategy for safe CI-driven apply.
- Replace app-level token cipher with managed secret/KMS-backed mechanism.

## 11. Potential Improvements (Deliberately Deferred for MVP)

- Token-at-rest cryptography hardening:
  - replace current app-level reversible token cipher with authenticated encryption + managed KMS/HSM-backed key lifecycle.
  - status: intentionally deferred; current implementation is accepted as an MVP tradeoff.

- Runtime secret strictness hardening:
  - remove insecure default fallbacks for sensitive settings (e.g. cipher/webhook secrets) and fail-fast on unsafe values outside local dev.
  - status: intentionally deferred; current defaults are accepted for MVP-only environments.
