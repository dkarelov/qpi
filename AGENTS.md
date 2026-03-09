# QPI AGENTS

Last updated: 2026-03-09 UTC

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
- `Задание` = buyer reservation/work item tied to one announcement.
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
- `libs/integrations/*`: WB/TonAPI/FX clients.
- `libs/config/settings.py`: runtime settings contracts.
- `libs/logging/setup.py`: YC-compatible structured logging.
- `libs/db/*`: pool and schema tooling.

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
- Seller cannot edit an existing announcement after creation; if parameters must change, the seller creates a new announcement and deletes the old one.
- Cashback is converted once to fixed `reward_usdt` at creation.
- Listing collateral requirement: `reward_usdt * slot_count * 1.01`.
- Listing activation/unpause requires:
  - valid WB token,
  - sufficient seller funds,
  - successful live WB metadata read for the stored `wb_product_id`.

### 4.2 Buyer rules

- Buyer enters shop by deeplink `shop_<slug>` or by saved shops menu.
- Buyer can reserve slot only on active listings.
- Buyer-facing primary CTA for an active listing is `Выполнить задание`.
- Buyer-facing listing screens show buyer-visible title, WB subject, brand, description, photo, sizes, characteristics, cashback in RUB with approximate percent, and `Цена` in RUB.
- Buyer receives setup token (base64 JSON array):
  - `[search_phrase, wb_product_id, 2]`.
- Buyer submits verification token (base64 JSON array):
  - `[order_id, ordered_at]`, where `ordered_at` is ISO datetime without timezone.
- Verification token must be submitted within 2 hours of reservation.
- `order_id` is globally unique (`1 order_id = 1 slot`).
- Buyer can cancel task in pre-submit states (`reserved`, `order_submitted`).
- One buyer cannot repeatedly buy the same target item:
  - duplicate reserve attempts are blocked,
  - already-bought item is not treated as a new available task.

### 4.3 Assignment lifecycle rules

Active states:

- `reserved`
- `order_submitted`
- `order_verified`
- `picked_up_wait_unlock`
- `eligible_for_withdrawal`
- `withdraw_pending_admin`
- `withdraw_sent`

Terminal/error states:

- `expired_2h`
- `wb_invalid`
- `returned_within_14d`
- `delivery_expired`

Transitions:

- `reserved -> expired_2h` after 2h without valid verification token.
- Valid verification token transitions to `order_verified`.
- WB event `Продажа` transitions to `picked_up_wait_unlock` and sets unlock time `pickup + 15d`.
- WB event `Возврат` within unlock window transitions to `returned_within_14d`.
- `order_verified -> delivery_expired` after 60 days without pickup.
- Unlock timer transitions to `eligible_for_withdrawal`.

### 4.4 Admin and finance rules

- Admin operations are Telegram-driven and auditable.
- Withdrawals require admin decision path:
  - open request,
  - approve/reject,
  - mark sent with tx hash.
- Manual deposit is supported for exception handling/bonuses/corrections.
- Manual deposit input supports role aliases:
  - `seller` maps to `seller_available`,
  - `buyer` maps to `buyer_available`.
- External reference for manual deposit is mandatory audit metadata and can be either:
  - free-form reason/comment,
  - tx reference (e.g. `tx:...`).

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
  - `withdrawn` or `token expired`.
- Token invalidation auto-pauses active listings via explicit application transaction (no PG trigger).

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
  - italic explanatory note at the bottom with next steps or issue guidance.
- Button labels include emoji/icon prefix.
- Each role opens with dashboard + section navigation.
- Seller UX:
  - `Объявления`, `Магазины`, `Баланс` are top sections,
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
- Seller balance screen shows `Всего`, `Свободно для новых объявлений`, and `Уже выделено под объявления`; activation shortfall is shown only when funds are insufficient.
- Seller top-up invoice screen:
  - shows the TON USDT address in copy-friendly monospace,
  - shows the exact `USDT` transfer amount in copy-friendly monospace,
  - includes two wallet actions:
    - Telegram Wallet home opener,
    - generic TON jetton transfer link for other wallets,
  - keeps the raw address and amount visible as fallback if wallet opening does not work.
- Transaction/history screens:
  - use representative `Транзакции ...` titles,
  - use `<` / `>` pagination when needed,
  - separate transaction blocks with empty lines,
  - use color indicators for statuses.
- Buyer UX:
  - shops/tasks/balance sections,
  - active listing CTA uses `Выполнить задание`,
  - task flow contains explicit submit-token and cancel-task actions.
- All user-facing timestamps are rendered in `MSK` (`Europe/Moscow`).
- Admin UX:
  - `Выводы`, `Депозиты`, `Исключения` sections.
- Sensitive inputs (tokens, payloads, withdrawal addresses) are deleted when possible.

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
- DB access: `psycopg3` + plain SQL only (no ORM).
- Schema changes only through `schema/schema.sql` + `psqldef`.
- Infrastructure mutations are Terraform-only from `infra/`.
- `yc` CLI is read-only for checks/debugging.
- Cloud Function packaging must be service-scoped to avoid unrelated redeploys.
- Bot runtime is webhook-based.
- Expected load target: ~100 concurrent users.

## 6. Infrastructure State (Current)

YC folder:

- `b1gmeblqlrrvm912n1uq`

Compute and networking:

- Bot runtime: instance group (`qpi-bot-ig`), size 1, preemptible.
- Bot public IP: `158.160.187.114`.
- DB VM: private-only (`10.131.0.28`), non-preemptible.
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
terraform -chdir=infra init
terraform -chdir=infra fmt
terraform -chdir=infra validate

TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
  infra/scripts/with_private_requirements.sh -- terraform -chdir=infra plan

TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
  infra/scripts/with_private_requirements.sh -- terraform -chdir=infra apply

terraform -chdir=infra output
```

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

### 7.3 Schema operations

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m libs.db.runtime_schema_compat apply
python -m libs.db.schema_cli plan
python -m libs.db.schema_cli apply
python -m libs.db.schema_cli drop
python -m libs.db.schema_cli export
```

Rule:

- Any bot release that starts reading new DB columns must apply schema before the bot process is restarted.
- For production-like legacy drift, run `python -m libs.db.runtime_schema_compat apply` before declarative `schema_cli apply`.
- Production schema apply should be executed from an operator machine or CI runner with `psqldef` available, using the standard SSH tunnel to `127.0.0.1:15432`.

### 7.4 Runtime smoke checks

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.bot_api.main --seller-command "/start" --telegram-id 10001 --telegram-username seller
python -m services.bot_api.main --buyer-command "/start" --telegram-id 10002 --telegram-username buyer
python -m services.daily_report_scrapper.main --once
python -m services.order_tracker.main --once
python -m services.blockchain_checker.main --once
```

### 7.5 Test runbook

```bash
# Shared venv for test tooling is expected at /home/darker/venv
# (includes pytest-asyncio and other dev dependencies).

# Main integration suite:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
pytest -q -m "not migration_smoke"

# Destructive migration smoke:
RUN_MIGRATION_SMOKE=1 \
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test_scratch \
pytest -q -m migration_smoke

# Telegram-emulated deterministic E2E suite:
PYTHONPATH=. uv run pytest -q tests/test_phase10_e2e_harness.py
```

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

- `.github/workflows/deploy_bot.yml`:
  - lint + tests,
  - schema drift regression against a PostgreSQL CI service,
  - additive runtime schema compatibility patch through SSH tunnel,
  - declarative production schema apply through SSH tunnel before rollout,
  - package bot artifact,
  - rollout to bot VM,
  - health verification,
  - post-deploy seller/buyer `/start` runtime smoke.
- `.github/workflows/deploy_terraform.yml`:
  - terraform validate/plan on push,
  - apply only via explicit manual dispatch guard.
- `.github/workflows/phase10_e2e.yml`:
  - deterministic Telegram-emulated E2E suite,
  - uploads JUnit artifact.

Private dependency handling:

- `requirements.txt` contains private dependency placeholder for `yc_json_logger` token.
- Use `infra/scripts/with_private_requirements.sh` for Terraform commands.
- Bot rollout script requires `TOKEN_YC_JSON_LOGGER` in rollout environment.

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
