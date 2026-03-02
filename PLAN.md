# QPI PLAN

Last updated: 2026-03-02 UTC

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
- Seller collateral top-up auto-confirmation via scheduled blockchain checker CF (planned in Phase 8).
- Manual admin operations remain for non-collateral credits and exception handling.
- Yandex Cloud-based infrastructure.

Out of scope (MVP):

- Disputes.
- Advanced wallet custody/security.
- Full generalized on-chain reconciliation for all inbound/outbound flows and auto-sweeping.

## 2.3 Core Actors

- Seller: creates shop/listings, provides WB token, funds collateral.
- Buyer: accepts listing slot, gets setup token for browser extension, submits base64 verification token, gets unlockable reward.
- Admin: handles exceptions/manual credits, approves withdrawals, monitors logs.

## 2.4 Functional Requirements

### Seller requirements

1. Seller registration/profile in bot.
2. Seller can create multiple shops, each with public deep link.
3. Seller can delete shops (soft delete only).
4. WB read-only token submission/validation.
5. Listing creation with:
   - `wb_product_id`
   - search phrase (seller scenario phrase in WB search)
   - cashback amount entered in RUB and converted to fixed USDT at creation
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
4. Buyer receives per-task setup token from bot (`[search_phrase, wb_product_id, 2]`, base64) and pastes it into browser extension.
5. Buyer must submit base64-encoded verification token from extension within 2 hours, else reservation expires.
6. Bot must decode verification token as JSON array `[order_id, ordered_at]` and validate:
   - `order_id` presence and uniqueness (`1 order_id = 1 slot`),
   - `ordered_at` parse/validity (ISO datetime without timezone).
7. MVP uses mock unsigned payload parsing/validation; tamper-protection/signature checks are post-MVP.
8. Valid token transitions assignment to `order_verified`.
9. Verification token payload contract for MVP is base64-encoded JSON array:
   - index `0`: `order_id` (string),
   - index `1`: `ordered_at` (ISO datetime string without timezone).
10. Unlock timer starts from WB pickup timestamp.
11. Reward unlock rule: 15 days after pickup.
12. If returned within 15 days: cancel reward.
13. After 15 days: return cancellation not needed (policy assumption).
14. If no pickup is detected within 60 days after `order_verified`, transition to `delivery_expired`.

### Admin/finance requirements

1. Seller collateral funding must support expected incoming transaction tracking with automatic confirmation.
2. Manual deposit credit by admin remains available for bonuses/corrections/exception handling.
3. Withdrawal requests require admin approve/reject.
4. Payouts are sent from hot wallet.
5. All admin and balance-changing actions are auditable.
6. Minimal admin control UI is acceptable (Telegram admin flow).

### Shared Telegram UX requirements

1. Every button press must produce visible feedback (`edit`, `reply`, or alert); silent no-op callbacks are not allowed.
2. Menus must follow a tree structure; avoid a single root screen with all actions.
3. Create/secondary actions must be nested in section screens (for example `Shops -> Create shop`, `Listings -> Create listing`, `Balance -> Deposit/History`).
4. Every button label must include a suitable emoji/icon prefix.
5. Each role must open with a role dashboard summary screen before showing action tree.
6. Seller dashboard minimum metrics:
   - shops total,
   - listings active/total,
   - orders in progress/completed/picked up,
   - balance free/total.
7. Admin accounts from `ADMIN_TELEGRAM_IDS` must be able to open seller/buyer modes in the same chat for operations/testing, without being blocked by strict single-role bootstrap checks.
8. In seller UX, shop actions must be two-level: `Магазины -> <название магазина> -> действия`; action buttons must not be rendered as a flat per-shop matrix on the list screen.
9. Shop rename must be available in seller UX. Rename must regenerate slug/deep link and must explicitly warn that old link stops working.
10. Seller-facing shop references must use shop names in UX; avoid exposing internal IDs/slugs in regular flow screens and action labels.
11. Active shop names must be unique per seller (case-insensitive) so name-based navigation remains unambiguous.
12. Buyer shops section must persist previously opened shops in PostgreSQL, so the saved shop list survives bot redeploy/restart.

## 2.5 Money and Pricing Rules

1. Ledger source of truth is USDT with fixed precision.
2. UI money format: `~350 руб. (4.55 USDT)`.
3. Listing stores fixed `reward_usdt`; seller input amount for cashback is RUB and conversion happens at listing creation time.
4. Full listing collateral is locked from seller balance; per-slot reserve occurs on buyer accept.
5. Listing collateral includes +1% buffer for transfer fees (`reward_usdt * slot_count * 1.01`).
6. Helper FX for `USDT` -> `RUB` is cache-driven: read from PostgreSQL first; if stale (>15 minutes), refresh on-demand from external API and persist back to PostgreSQL.

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
2. Strict DevOps policy for infrastructure:
   - all infrastructure mutations must be implemented in Terraform and applied from Terraform,
   - direct mutable `yc` operations are prohibited for normal delivery,
   - `yc` is read-only tool for debugging/checks/investigation.
3. Runtime decomposition target:
   - bot remains always-running VM service,
   - `daily-report-scrapper` runs as cloud function every 1 hour (Phase 5, first priority),
   - `order-tracker` runs as cloud function every 5 minutes (Phase 6, after Phase 5).
4. Service-to-service integration is database-mediated via PostgreSQL contracts.
5. MVP implementation mode for CF services is monorepo sub-services with Terraform-managed deployment from `infra/`.
6. Cloud function runtime changes (code/env/trigger/log wiring) are applied through Terraform, not mutable out-of-band deploy commands.
7. CF packaging/hashing must be service-scoped so unrelated repository files (for example root docs) do not force both function redeploys.
8. Dedicated repositories per CF are post-MVP optional after service contracts stabilize.
9. Access to VMs:
   - target model: OS Login,
   - temporary fallback in current state: bot and DB VMs use metadata-injected key-based SSH,
   - DB VM is private-only and is reached through SSH jump via bot VM,
   - local DB access for app/tests uses SSH local forward `127.0.0.1:15432 -> 10.131.0.28:5432` via bot VM, kept active during sessions and recreated if missing.
10. Initial expected load: about 100 concurrent users.
11. Deployment mode: webhook.
12. Initial zone: `ru-central1-d`.
13. Logging: structured logs to Yandex Logging.
14. One active infrastructure environment for MVP.

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
5. TON blockchain indexer API for incoming USDT transaction monitoring (Phase 8 target).
6. Yandex Cloud primitives: Compute, VPC, Logging, Cloud Functions.

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
     - JSON array required,
     - required shape `[order_id, ordered_at]`,
     - `ordered_at` must be ISO datetime without timezone (normalized to UTC on save).
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
     - base64 decode -> JSON parse -> array shape/type checks -> timestamp parse,
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
     - invalid token shape/types,
     - invalid timestamp,
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
4. Duplicate decoded `order_id` is rejected.
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
    - strict verification-token decode/validation (`[order_id, ordered_at]`, non-TZ ISO datetime),
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
7. Keep deployment Terraform-managed (`infra/serverless.tf`) for function code, env, log wiring, and timer trigger.
8. Reuse policy from `~/e-comet/reports`:
   - allowed: request/pagination/stream-parse logic patterns,
   - not allowed as foundation: full fork and ClickHouse/`aioscrapper`-coupled architecture.

Exit criteria:

1. Hourly CF ingests 3-day WB report window into PostgreSQL.
2. Re-run of same window is deduplicated and does not corrupt state.
3. Token invalidation + listing pause trigger only for required `401` message patterns.
4. Terraform apply deploys runnable CF runtime and logs are present in Yandex Logging.

Status:

- Completed in repository and deployed via Terraform.
- Implemented artifacts:
  - `libs/logging/setup.py`: migrated to `yc_json_logger`-backed wrapper so app logs emit YC-structured records (`message`, `level`, `logger`, extra fields).
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
  - `infra/serverless.tf`: function runtime packaging from service-scoped archive, env wiring, 1-hour trigger, log options.
  - CF runtime memory target applied as `128 MB`.
  - `.github/workflows/deploy_terraform.yml`: CI uses `infra/scripts/with_private_requirements.sh` for command-scoped private dependency rendering; push runs plan, apply is explicit `workflow_dispatch` input with guard until shared backend state is configured.
  - `infra/main.tf` + `infra/cloud-init/db.yaml.tftpl`: DB access wiring for CF source CIDR (`198.18.0.0/15`).
  - legacy GH deploy workflow removed; CF runtime delivery is Terraform-only.
  - expanded integration coverage in `tests/test_daily_report_phase5.py`.
- Validation on 2026-02-26 via active SSH tunnel:
  - `ruff check .` -> passed,
  - `TEST_DATABASE_URL=.../qpi_test pytest -q -m "not migration_smoke"` -> `23 passed, 1 deselected`,
  - `RUN_MIGRATION_SMOKE=1 TEST_DATABASE_URL=.../qpi_test_scratch pytest -q -m migration_smoke` -> `1 passed, 23 deselected`,
  - `DATABASE_URL=.../qpi_test TOKEN_CIPHER_KEY=... python -m services.daily_report_scrapper.main --once` -> successful runtime smoke.
  - live invoke in YC (2026-02-26): `{"shops_total": 1, "shops_processed": 1, "shops_failed": 0, ... , "ok": true}`.
- Follow-up on 2026-02-27 (observability hardening):
  - `libs/logging/setup.py` now inlines log fields into the `message` (`key=value`) so critical counters are visible in YC log rows without opening JSON details.
  - `daily-report-scrapper` now emits per-shop logs with explicit failure stage/severity and counters (`shop_id`, `rows_*`, `pages_fetched`, `final_rrd_id`, `status_code`, retry metadata).
  - live diagnosis became actionable: current `shops_failed=1` is `token_decrypt` (cipher key drift: shop token encrypted under `phase5-live-key`, current CF `TOKEN_CIPHER_KEY=change-me`).
- Follow-up on 2026-02-27 (key drift remediation):
  - `infra/variables.tf`: `cf_token_cipher_key` changed to required input (no default `change-me` fallback).
  - `.github/workflows/deploy_terraform.yml`: CI now passes `TF_VAR_cf_token_cipher_key` from secret `TOKEN_CIPHER_KEY` and validates required secrets before plan/apply.
  - live Terraform apply rotated CF env key to the canonical value; one-shot invoke verified recovery:
    - `{"shops_total":1,"shops_processed":1,"shops_failed":0,"rows_seen":47,"rows_upserted":2,...}`.

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
5. Keep deployment Terraform-managed (`infra/serverless.tf`) for function code, env, log wiring, and timer trigger.
6. MVP behavior: ignore correction operations (`Коррекция продаж`, `Коррекция возвратов`) in orchestration logic and track this as post-MVP TODO.

Exit criteria:

1. End-to-end automated transitions work across restarts.
2. No duplicate unlock/cancel events under retries.
3. Reservation expiry ownership is fully removed from VM worker runtime.
4. Terraform apply deploys runnable CF runtime and logs are present in Yandex Logging.

Status:

- Completed in repository and deployed via Terraform.
- Implemented artifacts:
  - `libs/logging/setup.py`: migrated to `yc_json_logger`-backed wrapper so app logs emit YC-structured records (`message`, `level`, `logger`, extra fields).
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
  - `infra/serverless.tf`: function runtime packaging from service-scoped archive, env wiring, 5-minute trigger, log options.
  - CF runtime memory target applied as `128 MB`.
  - legacy GH deploy workflow removed; CF runtime delivery is Terraform-only.
- `services/worker/main.py`: reservation-expiry ownership removed (worker tick is noop placeholder).
- integration coverage added in `tests/test_order_tracker_phase6.py`.
- Validation on 2026-02-26 via active SSH tunnel:
  - `ruff check .` -> passed,
  - `TEST_DATABASE_URL=.../qpi_test python -m pytest -q tests/test_order_tracker_phase6.py` -> `6 passed`,
  - `TEST_DATABASE_URL=.../qpi_test python -m pytest -q -m "not migration_smoke"` -> `34 passed, 1 deselected`,
  - `RUN_MIGRATION_SMOKE=1 TEST_DATABASE_URL=.../qpi_test_scratch python -m pytest -q -m migration_smoke` -> `1 passed, 34 deselected`,
  - `DATABASE_URL=.../qpi_test python -m services.order_tracker.main --once` -> successful runtime smoke.
  - live invoke in YC (2026-02-26): `{"lock_acquired": true, ..., "ok": true}`.
- Follow-up on 2026-02-27 (observability hardening):
  - `order-tracker` now logs phase-level counters and run duration in visible message text (`reservation`, `wb`, `delivery_expiry`, `unlock` phases).
  - lock-not-acquired completion is now logged as warning, not plain info.
- TODO (post-MVP):
  - define and implement correction-operation semantics in order tracking (`Коррекция продаж`, `Коррекция возвратов`).

## Phase 7: Full Product Go-Live (Telegram UX + Finance/Admin + Ops)

Goal:

- Deliver a fully usable production slice where seller and buyer can interact in live Telegram via buttons (not only text commands), while both cloud functions (`daily-report-scrapper`, `order-tracker`) run in production and finance/admin operations are executable end-to-end.

Scope lock:

1. Phase 7 delivers full live product operations for seller/buyer/admin plus observability baseline required to run the system safely day-to-day.
2. End-of-phase expectation is "real experience" with two real Telegram accounts (seller + buyer) and one admin account.
3. Command-style processors remain as internal/testing adapters; user-facing UX is button-first.

Execution streams:

1. Telegram transport runtime (real PTB webhook app)
   - Add a real `python-telegram-bot` application runtime for `services/bot_api`.
   - Keep existing domain services and command processors reusable, but add Telegram update adapters (messages, callbacks, deep links).
   - Implement webhook bootstrap with idempotent registration (`setWebhook`) and startup health checks.
   - Support direct-IP TLS webhook mode with self-signed certificate upload for Telegram webhook validation.
   - Introduce callback-data versioning (`v1:<flow>:<action>:<id>`) to keep button payloads stable and testable.
2. Role router and button-first UX shell
   - Add unified role-aware root menu (`seller`, `buyer`, `admin`) with Russian copy only.
   - Implement inline keyboards and stateful input prompts for each role.
   - Preserve deep-link entry (`/start shop_<slug>`) for buyer shop onboarding.
   - Ensure sensitive inputs (WB token, payout address, payload blobs) are deleted after parsing with explicit user notice.
3. Seller live UX completion
   - Convert seller lifecycle to guided button flow:
     - shop create/list/delete,
     - token set/replace with ping validation,
     - listing create/list/activate/pause/unpause/delete.
   - Keep existing soft-delete warnings and transfer split semantics in UI confirmations.
   - Add seller balance view (`seller_available`, `seller_collateral`) and listing collateral status visibility.
4. Buyer live UX completion
   - Convert buyer lifecycle to guided button flow:
     - open shop,
     - browse active listings,
     - reserve slot,
     - submit base64 payload,
     - track assignment statuses.
   - Add withdraw request UX:
     - select/enter amount,
     - enter payout address,
     - create `withdraw_pending_admin` request with idempotency guard.
   - Add buyer balance and withdrawal-history views.
5. Admin finance control plane (MVP manual ops)
   - Add admin access guard (`ADMIN_TELEGRAM_IDS` allowlist).
   - Implement admin queue screens:
     - pending withdrawals list/details,
     - approve,
     - reject with reason,
     - mark sent with `tx_hash`.
   - Implement manual deposit credit flow:
     - target account/user selection,
     - amount,
     - source reference (`tx_hash` or external note),
     - immutable audit event.
   - Send buyer notifications on withdraw state changes (approved/rejected/sent).
6. Finance domain and schema closure for live admin ops
   - Extend `libs/domain/ledger.py` with missing admin primitives:
     - manual deposit credit transaction (idempotent),
     - pending-withdrawal query API,
     - request detail query API for admin screens.
   - Add explicit DB contract for deposits (new table via `schema/schema.sql`), including:
     - `tx_hash`/external reference,
     - amount,
     - target account/user,
     - admin actor,
     - idempotency key,
     - timestamps.
   - Keep strict double-entry invariants and no direct balance writes outside transactional primitives.
7. Bot VM runtime deployment and CI/CD
   - Deploy real bot runtime on bot VM as managed service (systemd or container) with restart policy.
   - Add Terraform-managed runtime prerequisites on VM (service unit, env file path, health endpoint exposure).
   - Add CI workflow for bot rollout on `main`:
     - lint/tests gate,
     - artifact/package build,
     - rollout + health verification,
     - rollback hook.
   - Keep CF deployments Terraform-managed as currently implemented.
8. Launch observability and runbooks (mandatory in Phase 7)
   - Add correlation fields across bot + CF logs:
     - `telegram_update_id`,
     - `shop_id` / `listing_id` / `assignment_id`,
     - `withdrawal_request_id`,
     - `ledger_entry_id`.
   - Define logging queries/dashboard for:
     - webhook errors,
     - WB API failures,
     - pending-withdrawal backlog,
     - payout failure events.
   - Publish runbooks:
     - bot webhook outage,
     - CF failure/retry storms,
     - payout operation incident.

Exit criteria:

1. Live Telegram UX is button-driven for seller, buyer, and admin without requiring raw command syntax.
2. End-to-end business path works in production runtime:
   - seller flow -> buyer flow -> CF lifecycle -> withdrawal request -> admin completion.
3. Deposit and withdrawal operations are idempotent, fully auditable, and persisted with immutable ledger records.
4. Bot VM runtime has automated deployment, restart safety, and webhook health checks.
5. Phase 5/6 cloud functions remain healthy and integrated with live bot/DB state.
6. Observability/runbooks for bot/CF/finance operations are completed and validated.

Status:

- Completed in repository on 2026-02-27:
  - real PTB webhook runtime with callback contract and role router,
  - seller/buyer/admin button-first flows with stateful prompts and sensitive-input cleanup,
  - admin withdrawal control plane + manual deposit execution + notifications,
  - finance schema/domain closure for deposits and withdrawal query APIs,
  - bot VM runtime prerequisites + bot rollout CI with health check/rollback,
  - observability correlation fields and operational runbooks.
- Live rollout verified on 2026-02-27:
  - bot service healthy on VM (`qpi-bot.service`, `/healthz`),
  - webhook served on `https://158.160.187.114:8443/telegram/webhook` with uploaded custom certificate (`has_custom_certificate=true`),
  - DB schema applied on production DB with Phase 7 additions (`manual_deposits`, status/index updates).
- Next operational milestone: execute Phase 8 blockchain checker plan/implementation, then Phase 9 hardening + live Telegram UAT sign-off.

## Phase 8: Automated Collateral Deposit Confirmation (Blockchain Checker CF)

Goal:

- Remove manual seller collateral confirmation by introducing a scheduled Cloud Function (`blockchain-checker`, every 5 minutes) that monitors incoming USDT transfers and confirms expected deposits automatically.

Design baseline for planning:

1. Use shard deposit addresses managed by the service (pool model).
2. MVP starts with one shard address only; all seller top-up invoices route to this address.
3. Identify expected seller payments by amount suffix on top of rounded base amount:
   - `base_amount = ceil(required_amount_usdt * 10) / 10` (round up to 1 decimal),
   - `expected_amount = base_amount + suffix/10000`,
   - `suffix` is integer `001..999` (3-digit suffix space).
4. Invoice capacity is `999 * number_of_active_shards`; with one shard in MVP this is 999 active invoices.
5. Invoice TTL is fixed at 24 hours; suffix is released after credit/expiry/manual-cancel.
6. Keep manual admin fallback for ambiguity/failures.
7. Keep withdrawal flow unchanged in this phase.
8. Locked implementation profile:
   - network: TON mainnet,
   - provider: TonAPI,
   - confirmation threshold: 1,
   - matching precision: strict `received_amount >= expected_amount` with no epsilon.

Execution streams:

1. Expected-deposit contract and matching policy
   - Define `deposit_intent` lifecycle for seller collateral top-ups:
     - `pending` -> `matched` -> `credited` (success),
     - `pending` -> `expired`,
     - `pending`/`matched` -> `manual_review`.
   - On seller top-up request, generate expected amount using rounding + suffix:
     - compute base amount by rounding required amount up to 1 decimal,
     - allocate free suffix in `001..999` on shard,
     - compute `expected_amount = base_amount + suffix/10000`.
   - Enforce one active invoice per `(shard_id, suffix)` and TTL (`expires_at = created_at + 24h`).
   - Match rule for incoming tx:
     - suffix must point to an active invoice on that shard,
     - received amount must be `>= expected_amount`,
     - if received amount is above expected, credit full received amount and close invoice,
     - amount below expected does not close invoice.
   - Define idempotency rules for one intent -> one credit.
2. Seller bot UX for auto-confirmed top-up
   - Add seller action from balance/activation insufficiency path: "Пополнить".
   - Bot creates a `deposit_intent` and shows:
     - shard USDT address,
     - exact amount with suffix,
     - expiry time,
     - intent/reference ID.
   - Add "Проверить поступление" button for on-demand check trigger (optional fast path in addition to 5-minute CF).
   - Notify seller when credit is confirmed or intent expires/fails.
3. Schema and domain model
   - Add immutable expected-deposit table (for example `deposit_intents`) with:
     - seller/target account context,
     - shard context (`shard_id`, `deposit_address`),
     - expected amount,
     - suffix code (`001..999`) and rounded base amount,
     - status,
     - expiry,
     - matched tx linkage,
     - credited ledger linkage,
     - idempotency key and timestamps.
   - Add shard registry table (for example `deposit_shards`) to manage active/inactive shard addresses.
   - Add raw chain ingestion table (for example `chain_incoming_txs`) with:
     - chain/network token identifiers,
     - `tx_hash`,
     - `from_address`, `to_address`,
     - amount,
     - block/log cursor fields,
     - finality metadata,
     - raw payload,
     - processing status.
   - Extend finance domain with idempotent `confirm_expected_deposit(...)` primitive that posts into existing ledger/accounts (`seller_available`) and stores an immutable audit trail.
4. Blockchain integration client
   - Add `libs/integrations/<provider>.py` reader for incoming USDT transfers to shard addresses.
   - Use cursor-based incremental polling (`last_seen_cursor`) to avoid rescanning full history.
   - Normalize provider response into project transaction contract before DB insert.
   - Handle provider retry/backoff and rate limits.
5. New CF service: `blockchain-checker` (every 5 minutes)
   - Add service entrypoint `services/blockchain_checker/main.py`.
   - Add orchestration domain `libs/domain/blockchain_checker.py`:
     - advisory lock guard,
     - ingest new chain txs,
     - match txs to pending intents deterministically,
     - execute idempotent ledger credit on match,
     - mark intent/tx states,
     - emit structured counters/logs.
   - Add Terraform-managed function + timer trigger in `infra/serverless.tf` (cron `*/5 * ? * * *`).
6. Admin exception and recovery flow
   - Add admin queue for:
     - unmatched incoming txs,
     - expired intents,
     - ambiguous matches.
   - Provide admin actions:
     - attach tx to intent and credit,
     - cancel intent,
     - perform manual credit with explicit reason.
   - Preserve immutable audit records for every override.
7. Observability and operational controls
   - Add correlation IDs: `deposit_intent_id`, `chain_tx_id`, `tx_hash`, `ledger_entry_id`.
   - Add dashboards/queries:
     - pending intents older than SLA,
     - unmatched tx count,
     - CF scan lag,
     - provider API error rates.
   - Add runbooks for:
     - provider outage,
     - cursor desync/replay,
     - duplicate/ambiguous tx handling.
8. Verification and rollout
   - Add integration tests for:
     - happy-path auto credit,
     - duplicate invoke idempotency,
     - suffix pool exhaustion (all `001..999` busy on shard),
     - same-base collision handling via different suffixes,
     - expired intent handling,
     - admin override path.
   - Execute live UAT:
     - seller creates intent,
     - seller sends exact amount,
     - CF confirms and credits seller balance,
     - listing activation succeeds without manual admin deposit.

Exit criteria:

1. Seller collateral top-up can be auto-confirmed without manual blockchain checks by admin.
2. Every confirmed credit has linked records: `deposit_intent_id`, `suffix`, `tx_hash`, and `ledger_entry_id`.
3. Duplicate CF runs do not create duplicate credits.
4. Exception paths (unmatched/ambiguous/expired) are operable through admin flow with full audit.
5. Terraform-managed CF deployment and trigger are live and stable.

Status:

- Implemented in repository:
  - schema/domain/bot/CF/Terraform streams are coded,
  - integration tests for Phase 8 scenarios added (`tests/test_blockchain_checker_phase8.py`),
  - local non-DB checks passed; DB integration execution depends on `TEST_DATABASE_URL` availability in runtime environment.
- Pending operational step: apply Terraform and execute live Phase 8 rollout verification on production.

## Phase 9: Bot UX Information Architecture and Dashboards

Goal:

- Convert current flat role menus into a predictable, feedback-safe, dashboard-first UX tree for seller, buyer, and admin.

Execution streams:

1. UX interaction contract hardening (no silent callbacks)
   - Add explicit UX invariant at transport layer: every callback path must return one of:
     - edited message,
     - reply message,
     - alert/toast (`answerCallbackQuery` text).
   - Add fallback handlers for:
     - unknown action,
     - missing entity id,
     - empty data states.
   - Add telemetry counters/log events for callback outcomes (`handled`, `empty_state`, `fallback`, `error`) to detect regressions.
2. Seller IA refactor to tree menu
   - Seller root screen becomes dashboard + compact section buttons.
   - Move actions into section screens:
     - `Магазины` screen: shops list + `Создать магазин` action,
     - `Листинги` screen: listings list + `Создать листинг` action,
     - `Баланс` screen: summary + `Пополнить` + `Транзакции`.
   - Ensure empty states always respond (for example "магазинов пока нет").
3. Buyer IA refactor to tree menu
   - Buyer root screen becomes dashboard with key counters.
   - Section screens:
     - `Магазины`,
     - `Задания`,
     - `Баланс и вывод`.
   - Keep withdrawal steps guided and stateful while preserving sensitive-input deletion behavior.
4. Admin IA refactor to tree menu
   - Admin root screen becomes operations dashboard (pending withdrawals, deposit exceptions, recent actions).
   - Group actions into sections:
     - `Выводы`,
     - `Депозиты`,
     - `Исключения`.
   - Keep manual finance actions auditable and idempotent.
5. Emoji/icon design pass
   - Define fixed icon dictionary per role/section/action.
   - Apply icons to all inline buttons consistently (including back/menu actions).
   - Review labels for brevity and scanability in mobile Telegram UI.
6. Dashboard data contracts
   - Add explicit domain query APIs for dashboard aggregates where missing.
   - Seller dashboard contract:
     - shops total,
     - listings active/total,
     - orders in progress/completed/picked up,
     - balance free/total.
   - Define analogous compact contracts for buyer/admin dashboards.
7. Verification and rollout
   - Add callback UX tests asserting each button action returns visible feedback.
   - Add menu-structure tests to prevent flat-root regressions.
   - Execute live Telegram UX smoke with accounts (`seller`, `buyer`, `admin`) and capture evidence for each section path.

Exit criteria:

1. No known button path results in "nothing happened".
2. Root menus are compact and section-based (tree IA) for all roles.
3. All buttons contain suitable emoji/icon prefixes.
4. Each role opens to a dashboard summary screen with actionable section navigation below.
5. Regression tests cover callback feedback guarantees and tree navigation contract.

Status:

- Implemented in repository on 2026-02-27:
  - callback handling hardened to avoid silent no-op button behavior (`query.message` guard + explicit fallback path),
  - seller role now opens with dashboard summary and tree sections (`Магазины`, `Листинги`, `Баланс`),
  - create actions moved into section screens (`Создать магазин` under shops, `Создать листинг` under listings, top-up/history under balance),
  - buyer/admin roles now open from dashboard summaries with section-first navigation,
  - admin role-switch regression fixed: admin users can open seller/buyer modes without `non-seller/non-buyer role` bootstrap errors,
  - all inline button labels updated to emoji/icon-prefixed format.
- Additional UX refinement implemented on 2026-02-28:
  - seller shops screen changed to nested IA (`магазины список -> карточка магазина -> token/rename/delete`),
  - added shop rename flow with explicit warning about deep-link regeneration and old-link invalidation,
  - seller shop UX removed visible technical identifiers (`shop_id` / `slug`) from create/screen/action labels,
  - seller token prompt now shows full inline WB token instruction,
  - token sensitive-message cleanup no longer emits a duplicate generic deletion notice in token flow; success message is single and explicit,
  - post-action seller listing/shop paths now return to their current section view with notice instead of jumping back to root dashboard.
- Additional UX refinement implemented on 2026-02-28 (iteration 2):
  - seller `Создать магазин` flow switched to mandatory token-first sequence (validate token -> then request shop title),
  - newly created shops now persist the already validated token in the same UX flow (no extra post-create token step),
  - shop details now show token state in button label (`✅ Токен WB API` for valid, `❌ Токен WB API` otherwise),
  - shop deep-link messages render URL from a new line after `Ссылка для покупателей:` for Telegram width resilience,
  - dashboards for seller/buyer/admin no longer include static `Дашборд ...` title lines and now use compact metric rows,
  - dashboard money formatting standardized to `$USDT` with approximate helper `~RUB` (`DISPLAY_RUB_PER_USDT`, summary rounding),
  - seller/buyer terminology unified in UX text: `Обеспечение` and `Кэшбэк`.
- Additional UX refinement implemented on 2026-03-02:
  - buyer shops menu now uses PostgreSQL-backed persistence (`buyer_saved_shops`) instead of volatile in-memory-only “last shop” value,
  - opening a shop (deep link/code/saved entry) updates persistent buyer history and survives redeploy/restart,
  - buyer shops screen now renders persistent saved-shop buttons and keeps `open last` fallback from DB history.
- FX helper-rate upgrade implemented on 2026-02-28:
  - introduced PostgreSQL cache table `fx_rates` and bot-side `FxRateService`,
  - bot dashboards/balance screens now lazy-refresh `USDT_RUB` from CoinGecko only when cached value is older than TTL (`FX_RATE_TTL_SECONDS`, default 900),
  - concurrency for refresh is guarded by PostgreSQL advisory lock (`FX_RATE_REFRESH_LOCK_ID`),
  - failure mode uses latest cached rate; if cache is empty, falls back to `DISPLAY_RUB_PER_USDT`.
- Listing contract update implemented on 2026-03-01:
  - removed `discount_percent` from listing schema/domain and replaced with `search_phrase`,
  - seller listing input switched to one-line format with RUB cashback and quoted search phrase,
  - RUB cashback is converted to fixed `reward_usdt` at creation using current FX helper rate,
  - collateral requirement changed to `reward_usdt * slots * 1.01` (+1% fee buffer).
- Verification:
  - `ruff check services/bot_api/telegram_runtime.py tests/test_telegram_runtime_ux_phase9.py` passed.
  - `python -m py_compile services/bot_api/telegram_runtime.py tests/test_telegram_runtime_ux_phase9.py` passed.
  - Added menu-structure/emoji contract tests in `tests/test_telegram_runtime_ux_phase9.py`.
  - `PYTHONPATH=. uv run pytest -q tests/test_telegram_runtime_ux_phase9.py tests/test_bot_callback_contract.py` passed (`16 passed`).
  - `TEST_DATABASE_URL=... PYTHONPATH=. uv run pytest -q tests/test_fx_rates.py` passed.
- Live rollout verified on production bot VM (2026-02-27 UTC):
  - deployed release: `/opt/qpi/releases/20260227232627-phase9ux`,
  - bot service active and healthy (`/healthz` returns `ready=true`),
  - webhook endpoint is live on `https://158.160.187.114:8443/telegram/webhook` and returns expected `400` for invalid payload with valid secret token,
  - deployed runtime contains Phase 9 dashboard/tree/emoji menu code paths.
  - callback processing remains functional even when Telegram rejects `answerCallbackQuery` for stale callback IDs (`telegram_callback_answer_failed` warning + handler continues).

## Phase 10: Launch Hardening and UAT

Goal:

- Complete pre-launch hardening and formal UAT/sign-off after Phase 8 automation scope is finished.

Execution streams:

1. Launch hardening and readiness gate
   - Tighten SSH ingress from `0.0.0.0/0` to operator CIDRs before launch.
   - Enforce admin-only finance actions through allowlist + audit trail.
   - Verify secret alignment/rotation for:
     - `TOKEN_CIPHER_KEY`,
     - Telegram bot token,
     - webhook secret,
     - blockchain provider credentials.
   - Freeze go-live rollback plan (bot rollback + DB rollback + CF rollback actions).
2. Verification, UAT, and launch sign-off
   - Expand automated tests:
     - admin deposit flow,
     - withdrawal approve/reject/send matrix,
     - blockchain checker intent/match/recovery matrix,
     - idempotency/replay tests for admin/system actions,
     - Telegram callback contract tests,
     - end-to-end happy path from listing activation to `withdraw_sent`.
   - Execute UAT on live Telegram with real button flow:
     - seller creates and activates listing using auto-confirmed top-up,
     - buyer reserves/submits payload,
     - CFs move assignment to `eligible_for_withdrawal`,
     - buyer submits withdrawal request,
     - admin approves and marks payout sent.
   - Capture evidence in run log (timestamps, IDs, screenshots, tx hash placeholders).

Exit criteria:

1. Launch hardening controls are applied and validated.
2. UAT sign-off is completed with real Telegram interaction evidence.
3. Launch approval checklist is completed.

Status:

- Pending.

## 4. Recommended Execution Order

1. Finish remaining artifacts of Phase 0 (formal schema/state docs).
2. Phase 7 is implemented in repository (streams 1-8 complete).
3. Execute Phase 8 stream 1-8 (blockchain checker CF + auto-confirmed collateral top-ups).
4. Phase 9 live Telegram UX validation on production bot runtime is completed.
5. Execute Phase 10 stream 1-2 (hardening + UAT + launch sign-off).

## 5. Tracking Policy

On every relevant change:

- update `AGENTS.md` for decisions/state/runbook changes,
- update `PLAN.md` for requirement/phase/status changes,
- ensure both files remain consistent.
