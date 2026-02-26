# QPI PLAN

Last updated: 2026-02-26 UTC

## 1. Purpose

This file keeps:

- the detailed MVP requirements baseline,
- the phased implementation roadmap,
- current execution status.

`AGENTS.md` remains the operational/architecture state file.

## 2. Detailed Requirements Baseline

## 2.1 Product Goal

Build a minimal Telegram marketplace bot where WB sellers fund buyer rewards in USDT for completing a simplified buyout flow and leaving an honest review.

## 2.2 Scope

In scope:

- One Telegram bot (python-telegram-bot) with role-based flows for seller, buyer, admin.
- No Telegram Mini App.
- Russian-only UX.
- Manual admin operations for deposits and withdrawal approvals (MVP).
- Yandex Cloud-based infrastructure.

Out of scope (MVP):

- Disputes.
- Advanced wallet custody/security.
- Automatic deposit reconciliation from chain.

## 2.3 Core Actors

- Seller: creates shop/listings, provides WB token, funds collateral.
- Buyer: accepts listing slot, submits base64 plugin confirmation payload, gets unlockable reward.
- Admin: credits deposits manually, approves withdrawals, monitors logs.

## 2.4 Functional Requirements

### Seller requirements

1. Seller registration/profile in bot.
2. Seller can create multiple shops, each with public deep link.
3. Seller can delete shops (soft delete only).
4. WB read-only token submission/validation.
5. Listing creation with:
   - `wb_product_id`
   - discount percent (`10..100`)
   - reward amount in USDT
   - slot count `N`
6. Each shop can contain multiple listings.
7. Seller can delete listings (soft delete only).
8. Initial token validation must use live `GET https://statistics-api.wildberries.ru/ping`.
9. Token is persisted only after successful ping response.
10. Token ping validation must respect WB ping limits (3 requests per 30 seconds per domain).
11. MVP: product ownership check for listing creation is skipped (post-MVP TODO).
12. Listing activation requires full collateral for all `N` slots.
13. Listing auto-pauses if WB token becomes invalid/expired.
14. Deletion is not blocked by active listings/open assignments; bot must show warning before confirmation.
15. If deletion is confirmed:
    - assignment-linked reserved funds transfer to buyers immediately and irreversibly,
    - unassigned collateral is returned to seller.

### Buyer requirements

1. Buyer enters from shop deep link.
2. Buyer sees active listings and accepts one slot.
3. On accept, one reward slot is reserved/locked.
4. Buyer must submit base64-encoded purchase confirmation payload from browser plugin within 2 hours, else reservation expires.
5. Bot must decode payload using pre-agreed format and validate:
   - `order_id` presence and uniqueness (`1 order_id = 1 slot`),
   - `wb_product_id` matches listing,
   - `ordered_at` parse/validity.
6. MVP uses mock unsigned payload parsing/validation; tamper-protection/signature checks are post-MVP.
7. Valid payload transitions assignment to `order_verified`.
8. Mock payload contract for MVP (subject to change) is base64-encoded JSON:
   - `v` (int, payload version),
   - `order_id` (string),
   - `wb_product_id` (int),
   - `ordered_at` (RFC3339 UTC string).
9. Unlock timer starts from WB pickup timestamp.
10. Reward unlock rule: 15 days after pickup.
11. If returned within 15 days: cancel reward.
12. After 15 days: return cancellation not needed (policy assumption).
13. If no pickup is detected within 60 days after `order_verified`, transition to `delivery_expired`.

### Admin/finance requirements

1. Manual deposit credit by admin with tx hash/amount records.
2. Withdrawal requests require admin approve/reject.
3. Payouts are sent from hot wallet.
4. All admin and balance-changing actions are auditable.
5. Minimal admin control UI is acceptable (Telegram admin flow).

## 2.5 Money and Pricing Rules

1. Ledger source of truth is USDT with fixed precision.
2. UI money format: `~350 руб. (4.55 USDT)`.
3. Listing stores fixed `reward_usdt`; discount is listing metadata.
4. Full listing collateral is locked from seller balance; per-slot reserve occurs on buyer accept.

## 2.6 Assignment State Rules

Main states:

- `reserved`
- `order_submitted`
- `order_verified`
- `picked_up_wait_unlock`
- `eligible_for_withdrawal`
- `withdraw_pending_admin`
- `withdraw_sent`

Cancel/error states:

- `expired_2h`
- `wb_invalid`
- `returned_within_14d`
- `delivery_expired`

## 2.7 Non-Functional and Platform Requirements

1. Python-only implementation for bot and backend services.
2. Infrastructure created/changed via Terraform (avoid drift).
3. Runtime decomposition target:
   - bot remains always-running VM service,
   - `daily-report-scrapper` runs as cloud function every 1 hour (Phase 5, first priority),
   - `order-tracker` runs as cloud function every 5 minutes (Phase 6, after Phase 5).
4. Service-to-service integration is database-mediated via PostgreSQL contracts.
5. MVP implementation mode for CF services is monorepo sub-services with path-scoped CI/CD workflows.
6. CI/CD requirement: push to `main` must auto-deploy affected cloud function(s) without manual deploy steps.
7. Dedicated repositories per CF are post-MVP optional after service contracts stabilize.
8. Access to VMs:
   - target model: OS Login,
   - temporary fallback in current state: bot and DB VMs use metadata-injected key-based SSH,
   - DB VM is private-only and is reached through SSH jump via bot VM,
   - local DB access for app/tests uses SSH local forward `127.0.0.1:15432 -> 10.131.0.28:5432` via bot VM, kept active during sessions and recreated if missing.
9. Initial expected load: about 100 concurrent users.
10. Deployment mode: webhook.
11. Initial zone: `ru-central1-d`.
12. Logging: structured logs to Yandex Logging.
13. One active infrastructure environment for MVP.

## 2.8 Security/Operations Constraints (MVP)

1. Hot wallet with one key is accepted for MVP.
2. Sensitive chat inputs should be deleted after parsing with user notice.
3. Immutable accounting trail is required for all balance changes.

## 2.9 External Integrations

1. Telegram Bot API via python-telegram-bot.
2. Browser plugin output contract: buyer-provided base64 payload with order confirmation fields.
3. WB API for:
   - initial token validation via `GET https://statistics-api.wildberries.ru/ping`,
   - report ingestion (`reportDetailByPeriod`) for pickup/return signals and token invalidation events.
4. TON/USDT transfer path for withdrawals.
5. Yandex Cloud primitives: Compute, VPC, Logging, Cloud Functions.

## 2.10 Backend Persistence Baseline (Phase 2 Decision)

Assessment:

1. `asyncpg`:
   - best raw async PostgreSQL performance,
   - but introduces separate patterns for async runtime and operational tooling.
2. `psycopg3`:
   - one driver family for both async and sync use cases,
   - lower operational complexity for MVP,
   - performance is sufficient for expected load (~100 concurrent users).

Decision:

1. Service foundation is async for bot and background runtime paths.
2. PostgreSQL access driver is `psycopg3`.
3. Runtime DB access uses `psycopg.AsyncConnection` / `psycopg_pool.AsyncConnectionPool`.
4. Data access is plain SQL only (no ORM).
5. `psqldef` is the schema management tool and `schema/schema.sql` is the source of truth:
   - no manual DDL in PostgreSQL,
   - every schema change must be a reviewed update to `schema/schema.sql`,
   - all environments move schema only via `psqldef`.
6. Initial PostgreSQL state is clean (no schema objects), so baseline schema is bootstrapped from `schema/schema.sql`.

## 3. Implementation Plan

## Phase 0: Specification Lock

Deliverables:

1. Freeze state machine and transitions.
2. Freeze ledger model and accounting invariants.
3. Freeze contracts between bot handlers and scheduled orchestrator/scrapper services.

Exit criteria:

1. Approved state diagram.
2. Approved DB schema draft.
3. Approved timeout/failure handling rules.

Status:

- In progress (requirements clarified in chat; formal schema/state docs pending in codebase).

## Phase 1: Infrastructure Baseline (Terraform)

Deliverables:

1. Bot runtime in instance group (`size=1`, preemptible VM, static public IP).
2. Self-hosted PostgreSQL VM (`non-preemptible`, PostgreSQL 18).
3. Security groups, SA/IAM, logging group.
4. SSH access configured for operations (target OS Login, temporary bot + DB key-based fallback with DB jump via bot).
5. Private DB networking with NAT gateway for outbound internet.

Exit criteria:

1. `terraform apply` successful.
2. Bot IG healthy and auto-recovers.
3. DB has private-only interface and is reachable from bot SG.
4. Terraform state is clean (`plan` shows no changes).

Status:

- Completed.

## Phase 2: Backend Foundation (Detailed Plan)

Goal:

- Establish the code and database foundation for all later product phases, with strict schema governance and transactional safety.

Workstreams and deliverables:

1. Foundation skeleton and boundaries
   - Create package layout:
     - `services/bot_api`
     - `services/worker`
     - `libs/config`
     - `libs/db`
     - `libs/domain`
     - `libs/logging`
   - Define import boundaries so domain logic is reusable by bot handlers and worker jobs.
2. `psqldef` bootstrap and schema discipline
   - Add canonical schema definition (`schema/schema.sql`) and drop-safe empty schema (`schema/empty.sql`).
   - Configure schema execution against PostgreSQL 18 via `psqldef`.
   - Lock policy: schema changes only via `schema/schema.sql`; no direct DDL in DB.
   - Add schema checklist to PR expectations (`apply` + `drop` + `apply` on clean DB).
3. Baseline schema bootstrap (from clean DB)
   - Create foundational schema for:
     - identities/roles,
     - shops and WB token linkage metadata,
     - listings and slot accounting,
     - assignment lifecycle storage,
     - balances, holds/reserves, immutable ledger entries,
     - withdrawal requests, payout records, admin audit actions.
   - Add constraints/indexes for uniqueness and state integrity (`order_id`, slot ownership, idempotency keys).
4. DB access layer (`psycopg3`, plain SQL only)
   - Implement async connection pool setup and lifecycle hooks.
   - Provide transaction helper utilities (read-write/read-only, retry policy for serialization/deadlock cases).
   - Implement repository/query modules with parameterized SQL (no ORM/query builder dependency).
5. Ledger and reservation transactional primitives
   - Implement atomic SQL flows for:
     - collateral lock,
     - slot reserve/release,
     - reward unlock,
     - withdrawal request state transitions.
   - Enforce append-only ledger entries and explicit references to business events.
6. Configuration and structured logging baseline
   - Centralize settings loading for services and workers.
   - Add correlation IDs and DB operation context fields in logs.
7. Test baseline for data correctness
   - Schema smoke tests: empty DB -> `psqldef apply` -> `psqldef drop` -> `psqldef apply`.
   - Concurrency tests for double-reserve/double-spend prevention.
   - Ledger invariant tests (sum consistency, idempotent replays).
8. Developer runbook outputs
   - Document local/CI commands for migrations and DB checks.
   - Document rollback expectations for failed deployments.

Exit criteria:

1. Fresh PostgreSQL instance reaches target schema with `psqldef --apply`.
2. No schema drift: DB DDL matches `schema/schema.sql`.
3. All balance-changing operations are transactional and auditable.
4. Reservation/order uniqueness constraints are enforced at DB level.
5. Schema smoke tests pass in CI.
6. Core ledger invariants have automated tests.
7. Bot and worker can start with shared config/logging/db foundation.

Status:

- Completed in repository (service skeleton, `psqldef` schema baseline, plain-SQL `psycopg3` domain layer, and integration tests added).
- Runtime-validated on 2026-02-24 against target PostgreSQL via SSH tunnel:
  - `TEST_DATABASE_URL=... python -m pytest -q` -> `3 passed`,
  - clean DB path executed with `psqldef --apply` against baseline schema.
- Phase 3 is unblocked and ready to start.

## Phase 3: Seller Features (Detailed Execution Plan)

Goal:

- Deliver complete seller-side bot flow with strict listing activation rules and CF-ready contracts for token invalidation and order lifecycle integration.

Execution steps:

1. Contract freeze for seller + cross-service interfaces
   - Define seller command/handler contracts (onboarding, shop create/delete, token set, listing create/delete, listing activate/pause).
   - Define DB-level contracts consumed later by `order-tracker` and `daily-report-scrapper` (status enums, token status transitions, idempotency keys).
2. Schema evolution via `psqldef`
   - Add/adjust schema for seller lifecycle completeness:
     - listing activation metadata (`activated_at`, pause reason/source where needed),
     - explicit token validation metadata (`last_error`, status change source),
     - new order domain table baseline (normalized columns from plugin payload) to unblock Phase 4/5.
   - Validate clean path: `apply -> drop -> apply`.
3. Seller domain service implementation (plain SQL, `psycopg3`, async)
   - Add `SellerService` transactional primitives:
     - seller bootstrap + account guarantees,
     - shop creation + unique slug/deep-link payload generation,
     - shop/listing soft-delete operations with warning metadata and no blocking guards,
     - token save (encrypted) + status updates,
     - listing draft creation with invariant checks,
     - listing activation guarded by token validity + collateral sufficiency,
     - listing pause/unpause flows with explicit reason codes.
4. Minimal live token validation integration
   - Validate seller token at onboarding/update with `GET https://statistics-api.wildberries.ru/ping`.
   - Reject and do not persist token if ping is not successful.
   - Respect WB ping throttling limits in bot flow.
   - Keep implementation minimal (no broad multi-API WB client abstraction in MVP).
   - Track product ownership check as post-MVP TODO.
5. Bot handler integration for seller UX (Russian-only)
   - Add conversational flow in `services/bot_api`:
     - `/start` seller onboarding,
     - shop setup,
     - shop list/delete,
     - token submit/update,
     - listing creation wizard,
     - listing list/delete,
     - activation/pause controls and status rendering.
   - Show explicit irreversible-warning copy before delete confirmation when active/open entities exist.
   - Delete sensitive token messages after parsing and show cleanup notice.
6. Collateral and activation policy enforcement
   - MVP activation model: single command performs atomic collateral lock and switches status to `active` on success.
   - On insufficient collateral or invalid token, listing remains non-active with explicit user-facing reason.
   - Ensure idempotent retries for activation command.
   - On confirmed delete:
     - transfer assignment-linked reserved funds to buyers immediately and irreversibly,
     - return unassigned collateral to seller.
7. Token invalidation interoperability
   - Implement DB API used by `daily-report-scrapper` for token invalidation writes.
   - In scrapper invalidation transaction, explicitly update affected listings to `paused`.
   - Do not use PostgreSQL trigger for this in MVP (keep side effects explicit and observable).
   - Record provenance (`manual`, `scrapper_401_withdrawn`, `scrapper_401_token_expired`).
8. Test expansion (integration-first)
   - Seller onboarding/account bootstrap idempotency tests.
   - Shop slug uniqueness and deep-link generation tests.
   - Multi-shop/multi-listing CRUD tests with soft-delete semantics.
   - Delete confirmation warning tests for active listings/open assignments.
   - Confirmed-delete transfer split tests:
     - assignment-linked reserves -> buyers (irreversible),
     - unassigned collateral -> seller.
   - Token ping validation success/failure tests (token not persisted on failure).
   - Listing draft/activation success + failure matrix tests.
   - Idempotent activation/no double collateral lock tests.
   - Token invalidation auto-pause tests.
   - Keep existing Phase 2 integration suite green.
9. Ops and docs updates
   - Update service runbook and env var documentation.
   - Update architecture notes in `AGENTS.md` and status/progress in `PLAN.md`.
10. Phase completion validation
   - Run `python -m pytest -q` against PostgreSQL through active SSH tunnel.
   - Verify end-to-end seller scenario in bot:
     - onboard -> create shop -> set token -> create listing -> activate -> pause on token invalidation.

Exit criteria:

1. Listings activate only when both conditions are met:
   - WB token is valid,
   - seller available balance covers full listing collateral.
2. Activation is idempotent and cannot double-lock collateral.
3. Token invalidation (including scrapper-driven invalidation) automatically pauses active listings.
4. Seller flow is fully operable through Telegram bot commands.
5. Shop/listing create-delete flows work with soft-delete semantics and warning UX.
6. Confirmed deletion applies transfer split:
   - assignment-linked reserves to buyers (irreversible),
   - unassigned collateral to seller.
7. Integration tests cover seller success/failure paths and pass.

Status:

- Completed in repository.
- Implemented artifacts:
  - schema evolution in `schema/schema.sql` (seller lifecycle metadata + `buyer_orders` baseline),
  - `SellerService` plain-SQL transactional implementation (`libs/domain/seller.py`),
  - seller command handlers (`services/bot_api/seller_handlers.py`) and bot command execution path,
  - WB ping integration with rate limiting (`libs/integrations/wb.py`),
  - token reversible cipher helper (`libs/security/token_cipher.py`),
  - expanded integration coverage (`tests/test_seller_phase3.py`).
- Runtime validation on 2026-02-26 against target PostgreSQL via active SSH tunnel:
  - `TEST_DATABASE_URL=postgresql://qpi:***@127.0.0.1:15432/qpi python -m pytest -q` -> `10 passed`,
  - `DATABASE_URL=... python -m libs.db.schema_cli plan` -> `-- Nothing is modified --`,
  - `python -m services.bot_api.main --seller-command '/start' ...` command path succeeds with DB connectivity.

## Phase 4: Buyer Features

Goal:

- Deliver complete buyer-side flow from deep-link listing entry to `order_verified`, including deterministic reservation timeout handling and strict payload checks.

Execution steps:

1. Contract freeze for buyer + cross-service interfaces
   - Define buyer handler contracts (shop entry, listing browse, reserve slot, submit payload, status view).
   - Freeze payload validation contract for MVP mock payload:
     - base64 decode required,
     - JSON required,
     - required fields `v`, `order_id`, `wb_product_id`, `ordered_at`,
     - strict RFC3339 UTC parse for `ordered_at`.
   - Freeze transition contract:
     - successful validation moves assignment to `order_verified`,
     - `1 order_id = 1 slot` uniqueness remains DB-enforced.
2. Schema and DB-contract check via `psqldef`
   - Reuse/validate `buyer_orders` + `assignments` contract introduced in Phase 3.
   - Add only minimal indexes/constraints if needed for:
     - timeout polling (`reserved` + `reservation_expires_at`),
     - payload idempotency.
   - Validate clean path: `apply -> drop -> apply`.
3. Buyer domain service implementation (plain SQL, `psycopg3`, async)
   - Add `BuyerService` transactional primitives:
     - buyer bootstrap/account guarantees,
     - shop deep-link resolution by active shop slug,
     - active listing browse with slot availability projection,
     - slot reservation create/idempotent retry behavior,
     - payload submission/decode/validation + normalized order persistence,
     - assignment status listing for buyer dashboard.
4. Reservation and timeout behavior
   - Reservation path:
     - only active non-deleted listings are reservable,
     - reserve is atomic and decrements slots exactly once.
   - Timeout path (Phase 4 temporary runtime):
     - implement reservation expiry processor in `services/worker`,
     - transition stale `reserved` assignments to `expired_2h`,
     - release slot and reserved funds with existing transactional primitives.
   - Keep contract compatible with Phase 6 migration to `order-tracker` CF.
5. Payload submission validation path
   - Implement strict validation sequence:
     - base64 decode -> JSON parse -> field/type checks -> timestamp parse,
     - `wb_product_id` must match listing product,
     - reject duplicate `order_id`.
   - On success:
     - persist normalized order data in `buyer_orders`,
     - update assignment status to `order_verified`,
     - ensure idempotent re-submit behavior for same assignment/key.
6. Bot handler integration for buyer UX (Russian-only)
   - Add conversational flow in `services/bot_api`:
     - deep-link entry by shop slug,
     - listing browse,
     - reserve command/action,
     - payload submit command/action,
     - buyer assignments/status view.
   - Keep sensitive payload-input cleanup behavior aligned with MVP security requirements.
7. Test expansion (integration-first)
   - Buyer bootstrap and deep-link resolution tests.
   - Listing browse visibility tests (active vs paused/deleted).
   - Reservation success/concurrency/idempotency tests.
   - Timeout expiry processor tests (`reserved` -> `expired_2h` + rollback).
   - Payload validation matrix tests:
     - malformed base64,
     - invalid JSON,
     - missing/invalid fields,
     - invalid timestamp,
     - mismatched `wb_product_id`,
     - duplicate `order_id`.
   - Success path tests:
     - valid payload -> `order_verified`,
     - normalized `buyer_orders` row persisted.
   - Keep all Phase 2/3 integration tests green.
8. Ops and docs updates
   - Update runbook commands for buyer-flow smoke checks.
   - Update `AGENTS.md` and `PLAN.md` with runtime ownership of timeout job in Phase 4 and planned migration to Phase 6 CF.
9. Phase completion validation
   - Run `python -m pytest -q` against PostgreSQL through active SSH tunnel.
   - Verify buyer end-to-end scenario in bot:
     - deep-link -> browse -> reserve -> submit valid payload -> `order_verified`.

Exit criteria:

1. Buyer can reserve slot only on active listings with deterministic slot accounting.
2. Reservation timeout transitions are deterministic and rollback reserve effects correctly.
3. Invalid payloads are rejected with no incorrect state transitions.
4. Duplicate or mismatched decoded `order_id`/`wb_product_id` is rejected.
5. Valid payload transitions assignment to `order_verified` and stores normalized order record.
6. Buyer status view reflects assignment/order state accurately.
7. Integration tests cover buyer success/failure paths and pass.

Status:

- Completed in repository.
- Implemented artifacts:
  - `libs/domain/buyer.py`: plain-SQL `BuyerService` with:
    - buyer bootstrap/account guarantees,
    - deep-link shop resolution and active listing browse,
    - slot reservation/idempotency,
    - strict payload decode/validation (`v`, `order_id`, `wb_product_id`, `ordered_at` RFC3339 UTC),
    - normalized `buyer_orders` persistence and `order_verified` transition,
    - reservation expiry processor (`reserved` -> `expired_2h`).
  - `services/bot_api/buyer_handlers.py`: Russian buyer command handlers:
    - `/start` (`shop_<slug>` deep-link support), `/shop`, `/reserve`, `/submit_order`, `/my_orders`.
  - `services/bot_api/main.py`: buyer command execution path via `--buyer-command`.
  - `services/worker/main.py`: reservation expiry execution each tick; `--once` runs one expiry sweep.
  - `libs/domain/ledger.py`: reservation guard updated to reject deleted listings.
  - `schema/schema.sql`: timeout polling index
    - `idx_assignments_reserved_expires_at`.
  - expanded integration suite in `tests/test_buyer_phase4.py`.
  - test execution model hardened:
    - default integration suite no longer drops/recreates `public` per test,
    - schema is applied once and tables are truncated per test in dedicated test DB,
    - migration smoke (`apply/drop/apply`) is explicit opt-in (`RUN_MIGRATION_SMOKE=1`) and marked `migration_smoke`.
- Runtime validation on 2026-02-26 against target PostgreSQL via active SSH tunnel:
  - `TEST_DATABASE_URL=postgresql://qpi:***@127.0.0.1:15432/qpi python -m pytest -q` -> `21 passed`,
  - `DATABASE_URL=... python -m libs.db.schema_cli plan` -> `-- Nothing is modified --`,
  - `python -m services.bot_api.main --buyer-command '/start' ...` succeeds with DB connectivity,
  - `python -m services.worker.main --once` runs reservation expiry tick successfully.

## Phase 5: Daily Report Scrapper (Cloud Function)

Deliverables:

1. Implement `daily-report-scrapper` as a monorepo sub-service (`services/daily_report_scrapper`) using the existing QPI stack:
   - plain SQL,
   - `psycopg3` async pool/transactions,
   - existing token cipher and seller-domain invalidation primitives.
2. Add hourly cloud function runtime (Terraform + trigger + env wiring).
3. Fetch WB `reportDetailByPeriod` for the last 3 days with pagination support.
4. Persist raw report rows in PostgreSQL with idempotent writes (dedupe-safe re-runs).
5. Invalidate seller token only when WB response is HTTP `401` and message contains `withdrawn` or `token expired`:
   - use existing seller-domain transactional API,
   - auto-pause affected active listings in the same business transaction.
6. Add retry/backoff, bounded concurrency, and run-level idempotency/overlap guards.
7. Add CI/CD workflow: push to `main` auto-deploys this CF; no manual deploy steps.
8. Reuse policy from `~/e-comet/reports`:
   - allowed: request/pagination/stream-parse logic patterns,
   - not allowed as foundation: full fork and ClickHouse/`aioscrapper`-coupled architecture.

Exit criteria:

1. Hourly CF ingests 3-day WB report window into PostgreSQL.
2. Re-run of same window is deduplicated and does not corrupt state.
3. Token invalidation + listing pause trigger only for required `401` message patterns.
4. CI/CD redeploy on `main` push is active and validated.

Status:

- Completed in repository (application/runtime layer + tests + CI deploy workflow).
- Implemented artifacts:
  - `schema/schema.sql`: `wb_report_rows` with projected-only report fields and lookup indexes.
  - `libs/integrations/wb_reports.py`: WB `reportDetailByPeriod` client.
  - `libs/domain/daily_report.py`: Phase 5 orchestration:
    - valid-shop token selection,
    - 3-day pagination sync with retry (`period=daily`, `dateTo=yesterday`),
    - strict row projection to requested reduced column set only,
    - supplier operation allowlist (`Возврат`, `Продажа`, `Коррекция продаж`, `Коррекция возвратов`),
    - idempotent upsert to PostgreSQL,
    - token invalidation + listing pause on matching `401` messages.
  - `services/daily_report_scrapper/main.py`: cloud function handler + local `--once` runtime.
  - `.github/workflows/deploy_daily_report_scrapper.yml`: auto-deploy on `main` push.
  - expanded integration coverage in `tests/test_daily_report_phase5.py`.
- Validation on 2026-02-26 via active SSH tunnel:
  - `ruff check .` -> passed,
  - `TEST_DATABASE_URL=.../qpi_test pytest -q -m "not migration_smoke"` -> `23 passed, 1 deselected`,
  - `RUN_MIGRATION_SMOKE=1 TEST_DATABASE_URL=.../qpi_test_scratch pytest -q -m migration_smoke` -> `1 passed, 23 deselected`,
  - `DATABASE_URL=.../qpi_test TOKEN_CIPHER_KEY=... python -m services.daily_report_scrapper.main --once` -> successful runtime smoke.

## Phase 6: Order Tracker (Cloud Function)

Deliverables:

1. Implement `order-tracker` as a monorepo sub-service (`services/order_tracker`) with 5-minute trigger.
2. Migrate timeout ownership from VM worker to CF:
   - process reservation expiry (`reserved` -> `expired_2h`) in CF path.
3. Orchestrate assignment lifecycle post-`order_verified` using PostgreSQL contracts:
   - pickup detection from raw WB report data,
   - transition to `picked_up_wait_unlock`,
   - 15-day unlock checks to `eligible_for_withdrawal`,
   - return cancellation within 15 days (`returned_within_14d`),
   - timeout cancellation with `order_verified` -> `delivery_expired` after 60 days without pickup.
4. Enforce idempotent transitions and replay safety across CF retries/restarts.
5. Add CI/CD workflow: push to `main` auto-deploys this CF.
6. MVP behavior: ignore correction operations (`Коррекция продаж`, `Коррекция возвратов`) in orchestration logic and track this as post-MVP TODO.

Exit criteria:

1. End-to-end automated transitions work across restarts.
2. No duplicate unlock/cancel events under retries.
3. Reservation expiry ownership is fully removed from VM worker runtime.
4. CI/CD redeploy on `main` push is active and validated.

Status:

- Completed in repository (application/runtime layer + tests + CI deploy workflow).
- Implemented artifacts:
  - `schema/schema.sql`:
    - assignment status includes `delivery_expired`,
    - Phase 6 polling indexes added (`idx_assignments_order_tracking_order_id`, `idx_assignments_unlock_due`).
  - `libs/domain/order_tracker.py`:
    - 5-minute orchestration with advisory-lock overlap guard,
    - reservation timeout processing (`reserved` -> `expired_2h`) moved into CF ownership,
    - WB event matching by `srid = order_id` with MVP operation contract:
      - `Продажа` -> pickup transition (`order_verified` -> `picked_up_wait_unlock`) and unlock schedule (`+15 days`),
      - `Возврат` -> cancellation (`returned_within_14d`) when in allowed window,
      - `Коррекция продаж` / `Коррекция возвратов` ignored in MVP.
    - delivery timeout transition (`order_verified` -> `delivery_expired`) after 60 days without pickup.
    - reward unlock execution (`picked_up_wait_unlock` -> `eligible_for_withdrawal`).
  - `services/order_tracker/main.py`: cloud function handler + local `--once` runtime.
  - `.github/workflows/deploy_order_tracker.yml`: auto-deploy on `main` push.
- `services/worker/main.py`: reservation-expiry ownership removed (worker tick is noop placeholder).
- integration coverage added in `tests/test_order_tracker_phase6.py`.
- Validation on 2026-02-26 via active SSH tunnel:
  - `ruff check .` -> passed,
  - `TEST_DATABASE_URL=.../qpi_test python -m pytest -q tests/test_order_tracker_phase6.py` -> `6 passed`,
  - `TEST_DATABASE_URL=.../qpi_test python -m pytest -q -m "not migration_smoke"` -> `34 passed, 1 deselected`,
  - `RUN_MIGRATION_SMOKE=1 TEST_DATABASE_URL=.../qpi_test_scratch python -m pytest -q -m migration_smoke` -> `1 passed, 34 deselected`,
  - `DATABASE_URL=.../qpi_test python -m services.order_tracker.main --once` -> successful runtime smoke.
- TODO (post-MVP):
  - define and implement correction-operation semantics in order tracking (`Коррекция продаж`, `Коррекция возвратов`).

## Phase 7: Finance and Admin Controls

Deliverables:

1. Telegram admin queue with approve/reject actions.
2. Manual deposit credit flow (`tx_hash`, amount, target account).
3. Withdrawal request + admin approval + send flow.
4. Full finance audit trail.

Exit criteria:

1. Deposit/withdraw operations are auditable and idempotent.
2. Approved payout writes immutable ledger + tx records.

Status:

- Pending.

## Phase 8: Observability and Reliability

Deliverables:

1. Structured logs with correlation IDs.
2. Logging queries/dashboard for key errors.
3. Alerts/runbooks for CF orchestrators, WB integration, payout failures.
4. DB backup/restore runbook.

Exit criteria:

1. Critical flows observable and diagnosable.
2. Restore procedure tested at least once.

Status:

- Pending.

## Phase 9: Hardening and Launch

Deliverables:

1. Integration and smoke tests for core lifecycle.
2. Security tightening before launch (SSH CIDRs, secret rotation).
3. Go-live checklist + rollback plan.

Exit criteria:

1. UAT passed.
2. Operational checklist complete.
3. Launch approval.

Status:

- Pending.

## 4. Recommended Execution Order

1. Finish remaining artifacts of Phase 0 (formal schema/state docs).
2. Implement Phase 7 finance/admin controls for production money operations.
3. Complete Phases 8 and 9 before production launch.

## 5. Tracking Policy

On every relevant change:

- update `AGENTS.md` for decisions/state/runbook changes,
- update `PLAN.md` for requirement/phase/status changes,
- ensure both files remain consistent.
