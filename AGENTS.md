# QPI AGENTS

Last updated: 2026-02-27 UTC

## 1. Purpose and Maintenance Rules

This file is the operational source of truth for architecture decisions, infrastructure state, constraints, and runbooks.

Maintenance policy:

- Keep exactly two project docs:
  - `AGENTS.md`: decisions, constraints, deployed state, runbooks.
  - `PLAN.md`: detailed requirements baseline, phased implementation plan, and phase status.
- Update `AGENTS.md` and `PLAN.md` together when decisions, requirements, or execution status changes.
- Keep both files internally consistent with Terraform code and deployed infrastructure.

## 2. Product Scope (MVP)

Goal:

- Build a minimal Python-based Telegram marketplace bot where WB sellers fund buyer rewards in USDT for honest review flow completion.

In-scope:

- One Telegram bot (PTB) with role-based flows (seller, buyer, admin).
- Russian-only UX.
- No Telegram Mini App (anonymity requirement).
- WB integration for token validity and order/pickup/return checks.
- USDT ledger and payouts (TON ecosystem), with manual withdrawals and manual-exception finance ops.
- Planned in next phase: automated seller collateral top-up confirmation via blockchain checker CF.

Out-of-scope (MVP):

- Dispute handling.
- Full generalized on-chain reconciliation for all wallet flows.
- Advanced wallet security (multisig/HSM).

Detailed baseline requirements and phase-by-phase execution plan are tracked in `PLAN.md`.

## 3. Confirmed Product Decisions

### 3.1 Roles and entry points

- Single bot with role-based behavior (chosen for simplicity and reliability).

### 3.2 Listing and collateral

- Seller creates listings for full WB products (not samples).
- Seller can own multiple shops.
- Each shop can contain multiple listings.
- Seller bot flow must support create and delete for both shops and listings.
- Delete is soft delete only (no physical DB delete).
- Deletion is never blocked by active listings/open assignments.
- If active listings/open assignments exist, bot must show warning before delete confirmation.
- On confirmed delete:
  - assignment-linked reserved funds transfer to buyers immediately and irreversibly,
  - unassigned collateral is returned to seller.
- Seller sets discount from 10% to 100%.
- Seller must provide full collateral for all `N` slots before listing activation.
- Rewards are reserved per buyer/slot after accept.

### 3.3 Buyer assignment rules

- Buyer must submit a base64-encoded purchase confirmation blob from external browser plugin within 2 hours after slot reservation.
- Bot decodes payload (pre-agreed format) and validates `wb_product_id`, `order_id`, and `ordered_at`.
- MVP mock payload contract (subject to finalization): base64-encoded JSON with `v`, `order_id`, `wb_product_id`, `ordered_at` (RFC3339 UTC).
- MVP uses unsigned payload parsing/validation only; tamper-protection/signature validation is post-MVP.
- `1 order_id = 1 slot`.
- Decoded `order_id` must belong to listing `product_id`.
- Reservation timeout: 2 hours.
- Valid decoded payload moves assignment to `order_verified`.
- Unlock timer starts from WB pickup timestamp.
- Unlock period: 15 days after pickup.
- If returned within 15 days: cancel reward.
- After 15 days: do not cancel for return (per WB policy assumption).
- If no pickup is detected within 60 days after `order_verified`, assignment is cancelled as `delivery_expired`.

### 3.4 WB token handling (MVP)

- Initial token validation is live `GET https://statistics-api.wildberries.ru/ping` with seller-provided token in `Authorization` header.
- If initial ping fails, token is not stored in PostgreSQL and seller is asked to submit a valid token.
- Bot flow must respect WB ping limits (3 requests per 30 seconds per ping domain).
- Regular token checks are performed by `daily-report-scrapper` while requesting WB reports.
- If report request returns HTTP `401` and error detail contains `withdrawn` or `token expired`, token is invalidated in PostgreSQL.
- On token invalidation, listings are auto-paused via explicit SQL updates in application transaction (no PG trigger in MVP).
- Stored seller tokens are persisted as reversible application-level ciphertext using `TOKEN_CIPHER_KEY` (temporary MVP mechanism; KMS/HSM-backed secrets are post-MVP).

### 3.5 Finance flow (MVP)

- Current live state (Phase 7): deposits are credited manually by admin.
- Phase 8 target: seller collateral deposits are auto-confirmed from chain by expected-transaction matching; manual admin override remains.
- Withdrawals: buyer requests -> admin approval required -> payout.
- If fee policy changes, user should be notified.

### 3.6 Display and localization

- Money display format: `~350 руб. (4.55 USDT)`.
- Primary ledger currency: USDT.

### 3.7 Operations and moderation

- Minimal admin control panel is acceptable.
- Logging quality must be high (Yandex Logging).
- Sensitive inputs in chat should be deleted after parsing with user notice.

### 3.8 Backend foundation and migrations (Phase 2)

- Runtime foundation is async (bot + background paths).
- PostgreSQL access uses `psycopg3` as the primary driver family.
- Data access style is plain SQL only (no ORM).
- `psqldef`-first policy:
  - `schema/schema.sql` is the schema source of truth,
  - every DDL change is committed via `schema/schema.sql` and applied with `psqldef`,
  - direct/manual schema edits in PostgreSQL are not allowed.
- Baseline schema is defined in `schema/schema.sql`.

### 3.9 Runtime decomposition (post-Phase 2 target)

- Decompose backend into multiple microservices with DB-mediated contracts.
- Implementation mode for Phases 5-6: CF services are delivered as monorepo sub-services in this repository.
- Cloud Function runtime deployment is Terraform-managed in `infra/` (function config, service-scoped code package, env, logging, triggers).
- Separate repositories per service are optional post-MVP once contracts and deployment boundaries stabilize.
- Services exchange state via PostgreSQL tables/contracts (DB-mediated integration).
- Bot service remains always-on VM runtime.
- Reservation timeout ownership has been migrated to Phase 6 `order-tracker` CF (`reserved` -> `expired_2h`).
- Phase 5 target cloud function: `daily-report-scrapper` running every 1 hour:
  - requests WB `reportDetailByPeriod` for last 3 days,
  - stores raw dumps in PostgreSQL,
  - invalidates seller token in PostgreSQL on WB `401` with message containing `withdrawn` or `token expired`.
- Phase 6 cloud function: `order-tracker` orchestrator running every 5 minutes.

### 3.10 Phase 3 implementation baseline

- `schema/schema.sql` now includes seller lifecycle metadata:
  - shop token invalidation metadata (`wb_token_last_error`, `wb_token_status_source`, `wb_token_invalidated_at`),
  - shop/listing soft-delete fields (`deleted_at`, `deleted_by_user_id`),
  - listing activation/pause metadata (`activated_at`, `paused_at`, `pause_reason`, `pause_source`),
  - `buyer_orders` normalized order table baseline for upcoming buyer/plugin flow.
- `libs/domain/seller.py` is the plain-SQL transactional seller service:
  - seller bootstrap/account guarantees,
  - multi-shop/multi-listing create/list/delete,
  - listing activation/pause/unpause,
  - delete transfer split enforcement (assignment-linked -> buyer, unassigned -> seller),
  - token invalidation API for scrapper (`manual`, `scrapper_401_withdrawn`, `scrapper_401_token_expired`).
- `services/bot_api/seller_handlers.py` provides Russian seller command handlers:
  - `/start`, `/shop_*`, `/token_set`, `/listing_*`,
  - warning-before-confirm delete UX,
  - token ping check before persistence (reject on failure).
- `libs/integrations/wb.py` provides minimal WB ping client with in-process 3-per-30s throttling.

### 3.11 Phase 4 implementation baseline

- `schema/schema.sql` now includes timeout polling support index:
  - `idx_assignments_reserved_expires_at` (`reservation_expires_at` where `status='reserved'`).
- `libs/domain/buyer.py` is the plain-SQL transactional buyer service:
  - buyer bootstrap/account guarantees,
  - shop deep-link resolution and active listing browse,
  - slot reservation with idempotency,
  - strict base64 payload decode/validation (`v`, `order_id`, `wb_product_id`, `ordered_at` RFC3339 UTC),
  - `order_verified` transition with normalized `buyer_orders` persistence,
  - reservation expiry processor (`reserved` -> `expired_2h`) using existing finance transactional primitives.
- `services/bot_api/buyer_handlers.py` provides Russian buyer command handlers:
  - `/start` (including `shop_<slug>` deep-link payload),
  - `/shop`, `/reserve`, `/submit_order`, `/my_orders`,
  - sensitive payload input flagged for deletion.
- `services/worker/main.py` remains as VM worker placeholder runtime (Phase 6 migrated reservation expiry ownership to `order-tracker` CF).

### 3.12 Phase 5 implementation baseline

- `schema/schema.sql` now includes `wb_report_rows` with projected-only WB report columns:
  - `realizationreport_id`, `create_dt`, `currency_name`, `rrd_id`, `subject_name`, `nm_id`, `brand_name`, `sa_name`, `ts_name`, `quantity`, `retail_amount`, `office_name`, `supplier_oper_name`, `order_dt`, `sale_dt`, `delivery_amount`, `return_amount`, `supplier_promo`, `ppvz_office_name`, `ppvz_office_id`, `sticker_id`, `site_country`, `assembly_id`, `srid`, `order_uid`, `delivery_method`, `uuid_promocode`, `sale_price_promocode_discount_prc`.
- `libs/integrations/wb_reports.py` provides minimal async WB `reportDetailByPeriod` client.
- `libs/domain/daily_report.py` provides Phase 5 orchestration:
  - target-shop selection (valid tokens + non-deleted listings),
  - 3-day report sync with pagination/retry (`period=daily`, `dateTo=yesterday`),
  - strict row projection to `wb_report_rows`,
  - supplier operation allowlist (`Возврат`, `Продажа`, `Коррекция продаж`, `Коррекция возвратов`),
  - idempotent upsert (`ON CONFLICT (rrd_id, srid)`),
  - token invalidation on `401` message containing `withdrawn` / `token expired` via seller transactional API.
- `services/daily_report_scrapper/main.py` provides:
  - cloud function handler `services.daily_report_scrapper.main.handler`,
  - local CLI smoke mode (`--once`).
- Terraform serverless layer (`infra/serverless.tf`) now manages:
  - `qpi-daily-report-scrapper` function code package from service-scoped archive (daily-report service + shared runtime libs),
  - runtime env/log wiring,
  - 1-hour timer trigger.

### 3.13 Phase 6 implementation baseline

- `schema/schema.sql` now includes:
  - assignment status `delivery_expired`,
  - Phase 6 polling indexes:
    - `idx_assignments_order_tracking_order_id`,
    - `idx_assignments_unlock_due`.
- `libs/domain/order_tracker.py` provides Phase 6 orchestration:
  - advisory-lock guarded `run_once`,
  - reservation expiry processing in CF path (`reserved` -> `expired_2h`),
  - WB event-driven transitions from `wb_report_rows` by `srid = order_id`:
    - `Продажа` -> pickup (`order_verified` -> `picked_up_wait_unlock`) with `unlock_at = pickup_at + 15 days`,
    - `Возврат` -> cancellation (`returned_within_14d`) when return is within unlock window,
    - correction operations (`Коррекция продаж`, `Коррекция возвратов`) are ignored in MVP.
  - `order_verified` timeout cancellation to `delivery_expired` after 60 days without pickup.
  - reward unlock processing to `eligible_for_withdrawal`.
- `services/order_tracker/main.py` provides:
  - cloud function handler `services.order_tracker.main.handler`,
  - local CLI smoke mode (`--once`).
- Terraform serverless layer (`infra/serverless.tf`) now manages:
  - `qpi-order-tracker` function code package from service-scoped archive (order-tracker service + shared runtime libs),
  - runtime env/log wiring,
  - 5-minute timer trigger.
- `services/worker/main.py` no longer owns reservation expiry processing.

### 3.14 Phase 7 delivery target (locked)

- Phase 7 is the MVP go-live phase and must deliver a full usable product slice in live Telegram.
- End-of-phase user experience target:
  - seller and buyer can complete the core journey through button-driven Telegram UX,
  - admin can process deposits/withdrawals in Telegram,
  - both CF runtimes (`daily-report-scrapper`, `order-tracker`) operate on live data.
- Scope decision:
  - Phase 7 includes observability/runbook baseline for operations,
  - hardening + formal UAT/sign-off are tracked in Phase 10.
- Bot runtime target for Phase 7:
  - production PTB webhook application on bot VM,
  - command processors remain as internal/testing adapters, but user-facing interaction is button-first.
- Finance/admin target for Phase 7:
  - manual deposit credit with immutable audit records,
  - withdrawal queue approve/reject/send operations with idempotent transactional semantics and payout tx tracking.

### 3.15 Phase 7 implementation baseline

- Bot transport/runtime:
  - `services/bot_api/main.py` defaults to real webhook runtime (command processors remain for internal smoke/testing).
  - `services/bot_api/telegram_runtime.py` now provides:
    - PTB webhook runtime with idempotent `setWebhook`,
    - direct TLS webhook mode with cert/key paths (`WEBHOOK_TLS_CERT_PATH`, `WEBHOOK_TLS_KEY_PATH`) and self-signed cert upload during `setWebhook`,
    - callback contract parsing (`v1:<flow>:<action>:<id>`),
    - role-aware button shell (`seller`/`buyer`/`admin`),
    - stateful input prompts with sensitive message deletion notices,
    - built-in health endpoint (`/healthz`) on `BOT_HEALTH_PORT`.
- Seller live UX (buttons):
  - shop create/list/delete with warning/confirm previews and transfer split semantics,
  - WB token set/replace prompt with ping validation,
  - listing create/list/activate/pause/unpause/delete with warning/confirm previews,
  - balance and listing collateral visibility (`seller_available`, `seller_collateral`, locked/required collateral).
- Buyer live UX (buttons):
  - shop open/deep-link browse,
  - slot reserve from listing buttons,
  - payload submit prompt per assignment,
  - assignment status list,
  - withdrawal flow (full/custom amount -> payout address -> `withdraw_pending_admin` request),
  - buyer balance and withdrawal history views.
- Admin live UX (buttons):
  - pending withdrawal queue and per-request detail screen,
  - approve/reject(reason)/mark-sent(tx_hash) actions,
  - manual deposit command prompt (`telegram_id`, `account_kind`, `amount`, `external_reference`),
  - buyer Telegram notifications on withdrawal state changes.
- Finance/schema closure:
  - `schema/schema.sql` now includes `manual_deposits` immutable contract table.
  - `libs/domain/ledger.py` now includes:
    - `manual_deposit_credit` (idempotent + ledger/audit-backed),
    - `list_pending_withdrawals`,
    - `get_withdrawal_request_detail`,
    - `get_buyer_balance_snapshot`,
    - `list_buyer_withdrawal_history`.
- Bot deployment automation:
  - `infra/cloud-init/bot.yaml.tftpl` provisions `/etc/qpi/bot.env` and `qpi-bot.service`.
  - IP-mode webhook TLS baseline is self-signed:
    - cloud-init generates `/etc/qpi/webhook.crt` + `/etc/qpi/webhook.key` for bot public IP SAN,
    - bot runtime publishes webhook with uploaded certificate (`has_custom_certificate=true`),
    - webhook endpoint remains `https://<bot_public_ip>:8443/telegram/webhook`.
  - `.github/workflows/deploy_bot.yml` provides bot rollout pipeline:
    - lint/tests gate,
    - artifact rollout to VM,
    - health verification,
    - rollback-on-error hook.

### 3.16 Phase 8 implementation baseline: blockchain checker for collateral top-ups

- Objective:
  - remove manual admin blockchain checks for seller collateral funding.
- Runtime components added in repository:
  - domain service `libs/domain/deposit_intents.py` for expected-invoice lifecycle, shard registry, chain tx ingestion/matching helpers, and admin recovery actions,
  - integration client `libs/integrations/tonapi.py` (mainnet read path, optional API key, unauth throttle support),
  - orchestration service `libs/domain/blockchain_checker.py` with advisory-lock guarded 5-minute run,
  - Cloud Function entrypoint `services/blockchain_checker/main.py`.
- Matching contract implemented:
  - invoice amount formula: `base_amount = ceil(request_amount*10)/10`, `expected_amount = base_amount + suffix/10000`,
  - suffix space `001..999`, one active suffix per `(shard_id, suffix)` via partial unique index,
  - invoice `TTL=24h`,
  - deterministic auto-credit when `received_amount >= expected_amount` and payment is on-time,
  - overpayment credits full received amount,
  - partial/late payments route to `manual_review`.
- Schema contracts added in `schema/schema.sql`:
  - `deposit_shards`,
  - `deposit_intents`,
  - `chain_incoming_txs`,
  - `chain_scan_cursors`.
- Telegram UX additions (button-first):
  - seller: `Пополнить`, `Мои пополнения / Проверить`,
  - admin: `Исключения депозитов`, `Привязать tx -> intent`, `Отменить intent`.
- Terraform serverless wiring added:
  - function `qpi-blockchain-checker`,
  - timer trigger every 5 minutes,
  - runtime env for shard settings and TonAPI settings.
- Launch hardening and formal UAT/sign-off remain in Phase 10.

### 3.17 Phase 9 UX rules and implementation baseline

- Every inline button press must always produce visible feedback in chat:
  - message update, new reply, or alert,
  - silent no-op behavior is not allowed.
- Menu information architecture must be tree-structured:
  - no single-screen "all buttons at once" layout,
  - create actions are nested inside their domain sections (for example `Shops -> Create shop`, `Listings -> Create listing`, `Balance -> Deposit / History`).
- Every button label must start with a meaningful emoji/icon prefix.
- Each role must open to a role dashboard on first screen (before action buttons):
  - seller dashboard minimum fields: `магазинов всего`, `листинги активные / всего`, `заказы в процессе / совершенные / выкупленные`, `баланс свободный / общий`,
  - buyer and admin dashboards must provide analogous key summary counters for their role.
- Implemented in `services/bot_api/telegram_runtime.py`:
  - seller/buyer/admin first-screen dashboards with section navigation below,
  - tree-structured seller flow (`Магазины` -> `Создать магазин`, `Листинги` -> `Создать листинг`, `Баланс` -> `Пополнить`/`Мои пополнения`),
  - admin section routing (`Выводы`, `Депозиты`, `Исключения`) with dashboard summary,
  - callback guard for missing Telegram message context to prevent silent button no-op.

## 4. Functional Workflow Summary

Seller flow:

1. Register in bot.
2. Create shop and submit WB read-only token.
3. Seller token is checked live via `https://statistics-api.wildberries.ru/ping`; invalid token is rejected and not stored.
4. Create/delete shop(s), create/delete listing(s) per shop.
5. Create listing(s) with WB product binding, discount, reward, slots.
6. Delete uses soft-delete semantics and shows warning when active/open entities exist.
7. If deletion is confirmed:
   - assignment-linked reserved funds transfer to buyers immediately and irreversibly,
   - unassigned collateral is returned to seller.
8. Fund collateral and activate listing.
9. Share shop deep link in Telegram channels.

Buyer flow:

1. Open shop via deep link.
2. Accept available slot (funds reserved).
3. Submit base64-encoded plugin confirmation payload within 2 hours.
4. Bot decodes/validates payload and records normalized order fields (`order_id`, `wb_product_id`, `ordered_at`).
5. Valid payload moves assignment to `order_verified`; then order tracking continues for pickup/return.
6. After 15 days from pickup with no cancellation condition, reward becomes withdrawable.
7. Buyer requests withdrawal; admin approves; payout sent.

Automation checkpoints:

- Reservation timeout.
- Order payload validation and normalization.
- Raw WB report ingestion.
- Seller token invalidation on report API `401` (`withdrawn`/`token expired`).
- Pickup detection (via orchestrator + WB report data).
- 15-day unlock check.
- Return detection (within 15 days only).
- Delivery timeout cancellation (`order_verified` -> `delivery_expired`) after 60 days without pickup.

## 5. State Model (Assignment)

Core states:

- `reserved`
- `order_submitted`
- `order_verified`
- `picked_up_wait_unlock`
- `eligible_for_withdrawal`
- `withdraw_pending_admin`
- `withdraw_sent`

Cancel/failure states:

- `expired_2h`
- `wb_invalid`
- `returned_within_14d`
- `delivery_expired`

## 6. Technical and Platform Constraints

- All services in Python.
- Application data access: plain SQL via `psycopg3` (no ORM).
- Database schema management via `psqldef` + `schema/schema.sql` only.
- Runtime model: always-on bot VM + event/schedule-driven cloud functions for orchestrators/scrappers.
- Cloud function deployment policy: apply via Terraform from `infra/`; no out-of-band mutable deploy commands.
- Strict DevOps policy:
  - any infrastructure mutation (create/update/delete of cloud resources, IAM, schedules/triggers, networking, managed settings, runtime bindings) must be done via Terraform code in `infra/` and applied with Terraform,
  - direct mutable `yc` commands are prohibited for normal operations and delivery,
  - `yc` is allowed only for read-only checks/debugging/investigation.
- SSH access model:
  - temporary fallback: key-based SSH for bot and DB VMs (`ubuntu` + metadata `ssh-keys`, `enable-oslogin=false`),
  - DB VM is private-only and is accessed via SSH jump host through the bot VM,
  - local DB access for app/tests uses SSH local forward `127.0.0.1:15432 -> 10.131.0.28:5432` via bot VM, kept active during work sessions (recreate if missing),
  - OS Login should be restored for both VMs after root-cause fix.
- Target initial load: ~100 concurrent users.
- Deployment mode: Telegram webhook.
- Zone: `ru-central1-d`.
- Domain is not required for now (IP is acceptable for current stage).

## 7. Infrastructure Architecture (Current)

Folder:

- YC folder ID: `b1gmeblqlrrvm912n1uq`.

Compute:

- Bot runtime: instance group, size 1, preemptible VM, auto-heal.
- Bot VM shape: 2 vCPU, 2 GB RAM, 20 GB network-SSD.
- DB VM: non-preemptible, 2 vCPU, 4 GB RAM, 40 GB network-SSD.
- PostgreSQL target version: 18+ (current bootstrap path installs 18).
- Runtime components status:
  - `daily-report-scrapper` CF: deployed and Terraform-managed (hourly trigger).
  - `order-tracker` CF: deployed and Terraform-managed (5-minute trigger).
  - CF runtime memory: `128 MB` per function.
  - DB runtime access for CFs:
    - DB SG permits CF ingress on `5432`,
    - PostgreSQL `pg_hba.conf` includes serverless source CIDR `198.18.0.0/15`.

Network:

- Bot remains on default subnet with static public IP for webhook.
- DB moved to dedicated private subnet.
- Private subnet egress is via NAT gateway and route table.

Logging and access:

- Yandex Logging group enabled.
- Bot VM currently uses key-based SSH (`enable-oslogin=false`, metadata `ssh-keys`).
- DB VM currently uses key-based SSH (`enable-oslogin=false`, metadata `ssh-keys`) and is accessed through the bot jump host.

## 8. Deployed Resource Snapshot (As Implemented)

- Bot instance group: `cl17ilrmf3ukgtg14gbe` (`qpi-bot-ig`)
- Bot public IP: `158.160.187.114`
- DB instance: `fv4drfqh36622f5lf1vc` (`qpi-db`)
- DB private IP: `10.131.0.28`
- DB public IP: none
- NAT gateway: `enpkq1bnf0ij8jcjmf7s` (`qpi-nat-gw`)
- Private route table: `enpmdt4gs3gav0qd4nce` (`qpi-rt-private`)
- Private subnet: `fl8oled9cdd9u2efqaae` (`qpi-private-ru-central1-d`, `10.131.0.0/24`)
- Logging group: `e2345psnoc0appog5lil` (`qpi-prod-logs`)
- Daily report function: `d4ee0tvqv3jutd2kk3ng` (`qpi-daily-report-scrapper`)
- Daily report timer trigger: `a1siq0set9h5s5urpfcl` (`qpi-daily-report-scrapper-every-1h`, cron `0 * ? * * *`)
- Order tracker function: `d4edjmt28evde0urt9q4` (`qpi-order-tracker`)
- Order tracker timer trigger: `a1s1jvo6m2ncc5n0ql7t` (`qpi-order-tracker-every-5m`, cron `*/5 * ? * * *`)

Note:

- Run `terraform -chdir=infra output` for latest runtime values before operational actions.

## 9. Cost Notes (Reference, February 23, 2026)

From YC public price API used during planning:

- NAT gateway (`vpc.gateway.shared_egress_gateway.v1`): `0.39528 RUB/hour`.
- NAT egress surcharge SKU (`network.egress.nat`): `0 RUB/GB`.
- Public IP (`network.public_fips`): `0.26352 RUB/hour`.

Approximation at 730 h/month:

- NAT gateway: ~288.55 RUB/month.
- One public IP: ~192.37 RUB/month.

Interpretation:

- For a single host, NAT can be slightly more expensive than a single public IP.
- For multiple private hosts, NAT usually improves security posture and can become cost-efficient.

## 10. Terraform Runbook

Working directory:

- `infra/`

Common commands:

```bash
terraform -chdir=infra init
terraform -chdir=infra fmt
terraform -chdir=infra validate
# wrapper renders TOKEN_YC_JSON_LOGGER in-memory for the command and auto-restores requirements.txt
# TOKEN_YC_JSON_LOGGER can be explicit, or omitted when `gh auth token` is configured locally
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
  infra/scripts/with_private_requirements.sh -- terraform -chdir=infra plan
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
  infra/scripts/with_private_requirements.sh -- terraform -chdir=infra apply
terraform -chdir=infra output
```

Access:

```bash
yc compute instance-group list-instances --name qpi-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -i ~/.ssh/id_rsa ubuntu@158.160.187.114
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@10.131.0.28
```

DB local tunnel (session default):

```bash
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114
ss -ltnp | rg ':15432\\b'
```

Rule:

- Keep this tunnel active during the session unless explicitly asked to close it.
- If the listener is missing, recreate it with the command above before DB operations.

DB schema workflow (Phase 2 baseline):

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m libs.db.schema_cli plan
python -m libs.db.schema_cli apply
python -m libs.db.schema_cli drop
python -m libs.db.schema_cli export
```

Seller command smoke check:

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.bot_api.main --seller-command "/start" --telegram-id 10001 --telegram-username seller
```

Buyer command smoke check:

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.bot_api.main --buyer-command "/start" --telegram-id 10002 --telegram-username buyer
```

Worker smoke check (noop placeholder in current phase):

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.worker.main --once
```

Daily report scrapper smoke check:

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
export TOKEN_CIPHER_KEY=<cipher-key>
python -m services.daily_report_scrapper.main --once
```

Order tracker smoke check:

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.order_tracker.main --once
```

Test runbook:

```bash
# Use project test tooling from shared venv:
# /home/darker/venv (includes pytest-asyncio and other dev deps)

# Main integration suite (no schema drop/recreate per test):
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
pytest -q -m "not migration_smoke"

# Destructive migration smoke (explicit only):
RUN_MIGRATION_SMOKE=1 \
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test_scratch \
pytest -q -m migration_smoke
```

Phase 7 observability queries and runbooks:

- Core correlation fields in logs:
  - `telegram_update_id`
  - `shop_id`
  - `listing_id`
  - `assignment_id`
  - `withdrawal_request_id`
  - `ledger_entry_id`
- Logging query templates (Yandex Logging):
  - webhook errors:
    - service=`bot_api` and (`telegram_update_handler_failed` or HTTP webhook 4xx/5xx runtime errors),
    - group by `telegram_update_id` and error type.
  - WB API failures:
    - service in (`daily_report_scrapper`, `order_tracker`) and message contains `wb_api` or `daily_report_shop_failed_wb_api`.
  - pending-withdrawal backlog:
    - service=`bot_api` and admin queue events with `withdrawal_request_id`,
    - correlate with DB count of `withdraw_pending_admin`.
  - payout failure events:
    - service=`bot_api` and (`admin_withdraw_rejected`, `admin_withdraw_sent`, send failures),
    - include `withdrawal_request_id` and `tx_hash`.
- Runbook: bot webhook outage
  1. Check service state: `sudo systemctl status qpi-bot.service`.
  2. Check health endpoint locally: `curl -fsS http://127.0.0.1:18080/healthz`.
  3. Validate webhook registration via bot API (`getWebhookInfo`) and ensure URL matches runtime env.
  4. Rollback to previous release symlink in `/opt/qpi/releases` if latest rollout is broken.
- Runbook: CF failure/retry storm
  1. Inspect function logs for `daily_report_scrapper`/`order_tracker` by `request_id`.
  2. Confirm DB connectivity and token/key alignment (`TOKEN_CIPHER_KEY`) from env.
  3. Check timer triggers and recent invocation statuses.
  4. If failures are code-induced, deploy fixed revision via Terraform-managed pipeline.
- Runbook: payout operation incident
  1. Pull request detail by `withdrawal_request_id` (status, note, `tx_hash`, admin actor).
  2. Verify ledger postings and `manual_deposits`/`withdrawal_requests` audit trails.
  3. Notify affected buyer in Telegram and annotate reason in rejection note if needed.
  4. If funds state is inconsistent, pause further payout actions and escalate with DB snapshot evidence.

Rules:

- Never apply manual DDL directly in PostgreSQL.
- Validate every schema change on a clean DB path (`apply` -> `drop` -> `apply`).
- Test safety guardrails:
  - all tests require `TEST_DATABASE_URL` DB name containing `test`,
  - migration smoke additionally requires disposable DB naming (`scratch|tmp|disposable`) and `RUN_MIGRATION_SMOKE=1`.

## 11. Security and Risk Notes (MVP)

Accepted temporary risks:

- Hot wallet with one key.
- Broad SSH allowlist (`0.0.0.0/0`) for now.
- Broad DB SG ingress rule on `5432` (`0.0.0.0/0`) while DB remains private-only and serverless CIDR handling is stabilized.
- Manual finance operations.

Required controls even in MVP:

- Immutable ledger/audit records for balance-changing operations.
- Admin action audit trail (who/what/when).
- Sensitive input message cleanup in Telegram chats.

## 12. Open Items / Pending Inputs

- Production handling policy for secrets (wallet key/token lifecycle, rotation cadence).
- Optional TonAPI API key enablement for higher read throughput (current MVP mode supports unauth throttled polling).
- Final payout integration details and transaction broadcast implementation.
- Tightening SSH ingress from `0.0.0.0/0` to operator CIDRs before production launch.
- Optional migration from current self-signed IP TLS webhook to domain-managed trusted TLS.
- Final base64 payload contract from browser plugin:
  - canonical fields/types,
  - encoding details,
  - MVP mock payload is accepted now; final schema versioning is pending.
- Post-MVP payload integrity:
  - tamper-protection/signature mechanism.
- Post-MVP listing ownership check:
  - direct WB catalog/product endpoint vs cached report data.
- Post-MVP order-tracker semantics for WB correction operations:
  - `Коррекция продаж`,
  - `Коррекция возвратов`.
- Post-MVP decision on extracting CF services from monorepo into dedicated repositories.
- Terraform remote-state + CI apply strategy for fully automated CF redeploy on `main` (without out-of-band deploy actions).
- Replace temporary app-level token cipher with managed secret storage/KMS-backed encryption.

## 13. Change Log

- 2026-02-23: Initial Terraform baseline deployed (bot IG, DB VM, SGs, logging, static IP).
- 2026-02-23: DB moved to private-only subnet, NAT gateway + route table added.
- 2026-02-23: Documentation consolidated into this single `AGENTS.md` file.
- 2026-02-23: Added `PLAN.md` and split documentation responsibilities between `AGENTS.md` and `PLAN.md`.
- 2026-02-23: Phase 2 backend decisions locked (`async` runtime, `psycopg3`, plain-SQL data access) and runbook updated.
- 2026-02-23: Phase 2 implementation added in repo (service skeleton, baseline schema, plain-SQL transactional finance primitives, integration test suite).
- 2026-02-24: Bot VM SSH access switched to metadata key-based login (temporary OS Login fallback); direct `ssh` access verified.
- 2026-02-24: DB VM OS Login failure confirmed; DB VM recreated with metadata key-based SSH and accessed via bot jump host (pre-change snapshot: `fd89jf0i33f2v68bf0c0`).
- 2026-02-24: Phase 2 runtime verification completed on target DB via SSH tunnel (`python -m pytest -q`: 3 passed; clean DB schema apply path validated).
- 2026-02-24: Session policy added to keep DB SSH local tunnel (`127.0.0.1:15432`) always on unless explicitly closed.
- 2026-02-24: Migrated schema management from Alembic to `psqldef` (`schema/schema.sql` source of truth, Alembic files removed).
- 2026-02-26: Product flow updated to plugin base64 confirmation (buyer order validation) and target runtime decomposition into bot VM + CF orchestrators (`order-tracker`, `daily-report-scrapper`).
- 2026-02-26: Deletion policy locked: soft delete only, warning (no block) on active/open entities, and transfer split on confirmed delete (assignment-linked reserves -> buyers irreversible; unassigned collateral -> seller).
- 2026-02-26: Phase 3 implemented in repository:
  - schema evolved for seller lifecycle + `buyer_orders` baseline,
  - seller transactional domain service and bot seller command handlers added,
  - WB ping integration + token persistence guard added,
  - integration suite expanded and validated against tunneled PostgreSQL (`10 passed`).
- 2026-02-26: Phase 4 implemented in repository:
  - buyer transactional domain service and buyer command handlers added,
  - strict base64 payload validation and `order_verified` transition implemented,
  - reservation expiry processor added to worker runtime (`reserved` -> `expired_2h`),
  - schema timeout index added for reserved-expiry polling,
  - integration suite expanded and validated against tunneled PostgreSQL (`21 passed`).
- 2026-02-26: Test workflow hardened:
  - default integration suite switched from `drop/create public` to schema-apply-once + table truncate per test,
  - destructive migration smoke kept as explicit opt-in (`RUN_MIGRATION_SMOKE=1`) with disposable DB-name safety checks.
- 2026-02-26: Phase plan updated:
  - Phase 5/6 decomposition locked (`daily-report-scrapper` first, `order-tracker` second),
  - later phases shifted by +1 in `PLAN.md`,
  - CF delivery strategy locked to monorepo sub-services with auto-deploy CI/CD on `main` push.
- 2026-02-26: Phase 5 implemented in repository:
  - `wb_report_rows` projected-column table added in schema (PG-only storage contract),
  - WB report client + Phase 5 orchestration service added,
  - daily-report-scrapper runtime handler/CLI added,
  - auto-deploy GitHub workflow added for daily-report-scrapper CF,
  - integration suite expanded and validated against tunneled PostgreSQL (`23 passed`, `1 deselected`; migration smoke `1 passed`).
- 2026-02-26: Phase 6 implemented in repository:
  - `order-tracker` domain orchestration service and CF runtime added,
  - reservation timeout ownership migrated from VM worker to CF path,
  - WB event transitions implemented with MVP operation mapping (`Продажа` pickup, `Возврат` cancel, corrections ignored),
  - unlock window changed to 15 days and delivery timeout cancellation added (`order_verified` -> `delivery_expired` after 60 days),
  - auto-deploy GitHub workflow added for order-tracker CF,
  - integration coverage added in `tests/test_order_tracker_phase6.py`,
  - verification run completed on tunneled PostgreSQL (`34 passed`, `1 deselected`; migration smoke `1 passed`; order-tracker `--once` smoke successful).
- 2026-02-26: Cloud wiring for Phase 6 completed:
  - YC function `qpi-order-tracker` created (`d4edjmt28evde0urt9q4`) in folder `b1gmeblqlrrvm912n1uq`,
  - YC timer trigger `qpi-order-tracker-every-5m` created (`a1s1jvo6m2ncc5n0ql7t`, cron `*/5 * ? * * *`),
  - GitHub Actions secret `YC_ORDER_TRACKER_CF_ID` configured for repo `dkarelov/qpi`.
- 2026-02-26: Infrastructure governance policy tightened:
  - all DevOps infrastructure mutations are Terraform-only,
  - `yc` usage is restricted to read-only debugging/checks.
- 2026-02-26: Cloud Functions migrated to Terraform-managed runtime delivery:
  - `infra/serverless.tf` now deploys real CF handlers from repo source with runtime env, VPC connectivity, and logging options,
  - both CFs (`daily-report-scrapper`, `order-tracker`) are live and validated by direct invoke,
  - DB connectivity for CFs fixed by Terraform-managed DB SG ingress + PostgreSQL `pg_hba` rule for serverless CIDR `198.18.0.0/15`,
  - `terraform plan` is clean after apply (no drift).
- 2026-02-26: CF application logging format corrected for Yandex Logging UI:
  - `libs/logging/setup.py` migrated to `yc_json_logger`-backed wrapper, preserving `logger.info("event", key=value)` call style while emitting YC-structured fields (`message`, `level`, `logger`, payload extras),
  - deployment workflow now injects private `TOKEN_YC_JSON_LOGGER` into `requirements.txt` in CI workspace before Terraform apply (secret is not committed to repo).
- 2026-02-26: CF runtime packaging and sizing refined:
  - CF memory reduced from `256 MB` to `128 MB` for both `qpi-daily-report-scrapper` and `qpi-order-tracker` and applied in cloud,
  - Terraform packaging split into per-function service-scoped archives (instead of one whole-repo archive),
  - unrelated root docs/flows (for example `AGENTS.md` and `PLAN.md`) are excluded from CF package hashes, so they do not force both CF redeploys.
- 2026-02-27: CF observability hardening implemented:
  - logging messages now inline key fields as `key=value` in the visible message text (not only JSON extras),
  - `daily-report-scrapper` logs per-shop lifecycle/counters (`shop_id`, pages, rows, final `rrd_id`) and explicit failure stage/severity (`token_decrypt`, `wb_api`, `pagination_stall`),
  - `order-tracker` logs phase-level counters (reservation/WB/delivery/unlock) and run durations with warning on lock-not-acquired,
  - runtime diagnosis for current live shop failure: seller token decrypt failure caused by `TOKEN_CIPHER_KEY` drift (`phase5-live-key` at token creation vs current CF env `change-me`).
- 2026-02-27: `TOKEN_CIPHER_KEY` drift fix implemented and verified:
  - Terraform variable `cf_token_cipher_key` is now mandatory (no insecure default fallback),
  - CI workflow exports `TF_VAR_cf_token_cipher_key` from GitHub secret `TOKEN_CIPHER_KEY` and fails fast when missing,
  - GitHub repo secret `TOKEN_CIPHER_KEY` configured and daily-report CF redeployed with aligned key,
  - live invoke confirms recovery (`shops_failed=0`, shop processed successfully).
- 2026-02-27: Phase 7 planning scope locked for full live MVP experience:
  - `PLAN.md` Phase 7 expanded into detailed execution streams for Telegram button UX, admin finance controls, deployment, observability, hardening, and UAT,
  - prior planned launch-critical scope from Phases 8-9 merged into Phase 7 acceptance criteria,
  - `AGENTS.md` updated to reflect Phase 7 as the go-live phase target.
- 2026-02-27: Phase plan refined per launch sequencing update:
  - DB backup/restore drill removed from Phase 7 stream scope,
  - Phase 7 keeps execution streams 1-8 (functional go-live + observability/runbooks),
  - prior Phase 7 streams 9-10 (hardening + UAT/sign-off) moved to Phase 8.
- 2026-02-27: Phase 7 implementation completed in repository:
  - bot runtime migrated to real PTB webhook app with callback contract, role menus, stateful prompts, and sensitive-input cleanup,
  - seller/buyer/admin button flows implemented end-to-end (including admin withdrawal queue actions and buyer notifications),
  - finance domain/schema extended with `manual_deposits`, admin withdrawal query APIs, and idempotent manual deposit credit,
  - bot runtime now exposes `/healthz` and logs correlation fields (`telegram_update_id`, `shop_id`, `listing_id`, `assignment_id`, `withdrawal_request_id`, `ledger_entry_id` where applicable),
  - Terraform bot cloud-init now provisions `/etc/qpi/bot.env` + `qpi-bot.service`,
  - GitHub workflow `deploy_bot.yml` added for bot rollout with lint/tests gate, health verification, and rollback hook.
- 2026-02-27: Phase 7 live rollout verified on production bot VM:
  - bot runtime deployed as `qpi-bot.service` and healthy on `http://158.160.187.114:18080/healthz`,
  - webhook runtime switched to direct TLS with self-signed IP certificate (`/etc/qpi/webhook.crt`, `/etc/qpi/webhook.key`),
  - Telegram webhook registered with uploaded custom certificate (`has_custom_certificate=true`) at `https://158.160.187.114:8443/telegram/webhook`,
  - live DB schema applied via `schema_cli` including `manual_deposits` and Phase 7 state/index updates.
- 2026-02-27: Phase roadmap updated for automated collateral top-up confirmation:
  - new Phase 8 added for `blockchain-checker` CF planning/implementation (every 5 minutes),
  - prior Phase 8 hardening/UAT moved to Phase 9 and prior reserved Phase 9 shifted to Phase 10,
  - finance scope clarified: manual admin deposits remain for exceptions, while seller collateral top-ups move to expected-transaction auto-confirmation target.
- 2026-02-27: Phase 8 top-up matching contract locked:
  - chosen model is amount suffix on shard address pool (not per-intent addresses),
  - suffix space is `001..999` with 24h TTL invoices and one active suffix per `(shard_id, suffix)`,
  - amount formula is `ceil(required*10)/10 + suffix/10000`,
  - MVP starts with one shard address (`999` concurrent invoices cap).
- 2026-02-27: Phase 8 repository implementation completed:
  - added expected-deposit schema contracts (`deposit_shards`, `deposit_intents`, `chain_incoming_txs`, `chain_scan_cursors`),
  - implemented seller top-up invoice flow and admin exception actions in Telegram runtime,
  - implemented TonAPI integration client and `blockchain-checker` orchestration service/entrypoint,
  - wired Terraform-managed `qpi-blockchain-checker` function and 5-minute trigger with configurable shard/TonAPI runtime env.
- 2026-02-27: Private dependency deploy path hardened:
  - added `infra/scripts/with_private_requirements.sh` wrapper to render `TOKEN_YC_JSON_LOGGER` only for command scope and always restore `requirements.txt`,
  - Terraform CI workflow now uses the wrapper for `plan/apply` instead of inline ad-hoc token replacement logic,
  - bot rollout now passes `TOKEN_YC_JSON_LOGGER` only to rollout-time `pip install` on VM (release artifact keeps placeholder form),
  - `infra/scripts/remote_rollout_bot.sh` now fails fast when private dependency placeholder is present but token is missing.
- 2026-02-27: Terraform CI behavior corrected for local-state backend:
  - push pipeline now runs Terraform plan only,
  - Terraform apply is available only via `workflow_dispatch` with explicit `apply=true`,
  - workflow now fails fast for manual apply attempts when shared backend state is not configured in CI.
- 2026-02-27: Bot deploy CI hardening completed:
  - rollout artifact packaging switched to `git archive` to avoid transient `tar: file changed as we read it` failures,
  - SSH key prep in workflow now supports multiline, escaped `\\n`, and base64-encoded secret forms with validation before `scp/ssh`,
  - repository secret `BOT_VM_SSH_PRIVATE_KEY` rotated to base64 form for stable GitHub Actions parsing.
- 2026-02-27: UX refinement rules locked for next phase:
  - every button click must always return visible feedback (no silent callbacks),
  - bot menus must be tree-structured (no flat "F16 panel" root),
  - all button labels require emoji/icon prefixes,
  - each role must open from a dashboard summary screen with key role metrics.
- 2026-02-27: Phase numbering update applied:
  - Phase 9 and Phase 10 switched,
  - UX information-architecture/dashboard refinement is now Phase 9,
  - launch hardening + UAT/sign-off is now Phase 10.
- 2026-02-27: Phase 9 UX refinement implemented in repository:
  - Telegram runtime menus are dashboard-first and section-tree structured for seller/buyer/admin,
  - seller `Создать` and `Баланс` actions moved under their corresponding sections,
  - all inline button labels now have emoji/icon prefixes,
  - callback handler now guards missing message context to prevent silent button behavior,
  - UX contract tests added in `tests/test_telegram_runtime_ux_phase9.py`.
- 2026-02-27: Local tooling clarification:
  - test/dev dependency execution is expected from shared venv `/home/darker/venv` (includes `pytest-asyncio` and other `.[dev]` packages).
- 2026-02-27: Phase 9 bot UX rollout deployed and verified on production:
  - bot release `/opt/qpi/releases/20260227232627-phase9ux` is active,
  - health check passed on `http://158.160.187.114:18080/healthz`,
  - webhook listener on `:8443` is active and serves expected validation errors for malformed updates with correct secret token,
  - deployed runtime confirms dashboard/tree/emoji menu changes are present.
- 2026-02-27: Callback resilience hardening deployed:
  - bot now logs warning and continues callback handling if `answerCallbackQuery` fails (for example stale callback IDs),
  - this prevents callback flow abortion on Telegram callback-answer timeout edge cases.
