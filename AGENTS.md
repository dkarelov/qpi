# QPI AGENTS

Last updated: 2026-02-26 UTC

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
- USDT ledger and payouts (TON ecosystem), with manual ops for MVP.

Out-of-scope (MVP):

- Dispute handling.
- Automated on-chain deposit reconciliation.
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
- Unlock period: 14 days after pickup.
- If returned within 14 days: cancel reward.
- After 14 days: do not cancel for return (per WB policy assumption).

### 3.4 WB token handling (MVP)

- Initial token validation is live `GET https://statistics-api.wildberries.ru/ping` with seller-provided token in `Authorization` header.
- If initial ping fails, token is not stored in PostgreSQL and seller is asked to submit a valid token.
- Bot flow must respect WB ping limits (3 requests per 30 seconds per ping domain).
- Regular token checks are performed by `daily-report-scrapper` while requesting WB reports.
- If report request returns HTTP `401` and error detail contains `withdrawn` or `token expired`, token is invalidated in PostgreSQL.
- On token invalidation, listings are auto-paused via explicit SQL updates in application transaction (no PG trigger in MVP).
- Stored seller tokens are persisted as reversible application-level ciphertext using `TOKEN_CIPHER_KEY` (temporary MVP mechanism; KMS/HSM-backed secrets are post-MVP).

### 3.5 Finance flow (MVP)

- Deposits: manual credit by admin.
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
- CI/CD mode for CF services: push to `main` auto-deploys affected functions (no manual deploy step).
- Separate repositories per service are optional post-MVP once contracts and deployment boundaries stabilize.
- Services exchange state via PostgreSQL tables/contracts (DB-mediated integration).
- Bot service remains always-on VM runtime.
- Phase 4 temporary ownership: reservation timeout (`reserved` -> `expired_2h`) is executed in the VM worker service.
- Phase 5 target cloud function: `daily-report-scrapper` running every 1 hour:
  - requests WB `reportDetailByPeriod` for last 3 days,
  - stores raw dumps in PostgreSQL,
  - invalidates seller token in PostgreSQL on WB `401` with message containing `withdrawn` or `token expired`.
- Phase 6 target cloud function: `order-tracker` orchestrator running every 5 minutes.

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
- `services/worker/main.py` now executes reservation expiry processing each tick (Phase 4 temporary runtime owner until Phase 6 `order-tracker` CF).

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
6. After 14 days from pickup with no cancellation condition, reward becomes withdrawable.
7. Buyer requests withdrawal; admin approves; payout sent.

Automation checkpoints:

- Reservation timeout.
- Order payload validation and normalization.
- Raw WB report ingestion.
- Seller token invalidation on report API `401` (`withdrawn`/`token expired`).
- Pickup detection (via orchestrator + WB report data).
- 14-day unlock check.
- Return detection (within 14 days only).

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

## 6. Technical and Platform Constraints

- All services in Python.
- Application data access: plain SQL via `psycopg3` (no ORM).
- Database schema management via `psqldef` + `schema/schema.sql` only.
- Runtime model: always-on bot VM + event/schedule-driven cloud functions for orchestrators/scrappers.
- Cloud function deployment policy: automatic deploy on `main` push via CI/CD workflows.
- Infrastructure changes via Terraform only (avoid drift).
- YC CLI allowed for checks/debugging only.
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
- Planned next runtime components (not yet deployed): `daily-report-scrapper` CF (1-hour trigger, Phase 5), `order-tracker` CF (5-minute trigger, Phase 6).

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
YC_TOKEN="$(yc config get token)" terraform -chdir=infra plan
YC_TOKEN="$(yc config get token)" terraform -chdir=infra apply
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

Worker reservation timeout smoke check:

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
python -m services.worker.main --once
```

Test runbook:

```bash
# Main integration suite (no schema drop/recreate per test):
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
pytest -q -m "not migration_smoke"

# Destructive migration smoke (explicit only):
RUN_MIGRATION_SMOKE=1 \
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test_scratch \
pytest -q -m migration_smoke
```

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
- Manual finance operations.

Required controls even in MVP:

- Immutable ledger/audit records for balance-changing operations.
- Admin action audit trail (who/what/when).
- Sensitive input message cleanup in Telegram chats.

## 12. Open Items / Pending Inputs

- Production handling policy for secrets (wallet key/token lifecycle, rotation cadence).
- Final payout integration details and transaction broadcast implementation.
- Tightening SSH ingress from `0.0.0.0/0` to operator CIDRs before production launch.
- Optional domain/TLS strategy if webhook setup is hardened later.
- Final base64 payload contract from browser plugin:
  - canonical fields/types,
  - encoding details,
  - MVP mock payload is accepted now; final schema versioning is pending.
- Post-MVP payload integrity:
  - tamper-protection/signature mechanism.
- Post-MVP listing ownership check:
  - direct WB catalog/product endpoint vs cached report data.
- Post-MVP decision on extracting CF services from monorepo into dedicated repositories.
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
