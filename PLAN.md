# QPI PLAN

Last updated: 2026-02-23 UTC

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
- Buyer: accepts listing slot, submits order ID, gets unlockable reward.
- Admin: credits deposits manually, approves withdrawals, monitors logs.

## 2.4 Functional Requirements

### Seller requirements

1. Seller registration/profile in bot.
2. Shop creation with public deep link.
3. WB read-only token submission/validation.
4. Listing creation with:
   - `wb_product_id`
   - discount percent (`10..100`)
   - reward amount in USDT
   - slot count `N`
5. Listing `wb_product_id` must belong to seller WB account.
6. Listing activation requires full collateral for all `N` slots.
7. Listing auto-pauses if WB token becomes invalid/expired.

### Buyer requirements

1. Buyer enters from shop deep link.
2. Buyer sees active listings and accepts one slot.
3. On accept, one reward slot is reserved/locked.
4. Buyer must submit `order_id` within 2 hours, else reservation expires.
5. `order_id` rules:
   - one `order_id` can be used only once,
   - order must match listing `wb_product_id`.
6. Unlock timer starts from WB pickup timestamp.
7. Reward unlock rule: 14 days after pickup.
8. If returned within 14 days: cancel reward.
9. After 14 days: return cancellation not needed (policy assumption).

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

## 2.7 Non-Functional and Platform Requirements

1. Python-only implementation for bot and backend services.
2. Infrastructure created/changed via Terraform (avoid drift).
3. OS Login enabled and used for access (`yc compute ssh`).
4. Initial expected load: about 100 concurrent users.
5. Deployment mode: webhook.
6. Initial zone: `ru-central1-d`.
7. Logging: structured logs to Yandex Logging.
8. One active infrastructure environment for MVP.

## 2.8 Security/Operations Constraints (MVP)

1. Hot wallet with one key is accepted for MVP.
2. Sensitive chat inputs should be deleted after parsing with user notice.
3. Immutable accounting trail is required for all balance changes.

## 2.9 External Integrations

1. Telegram Bot API via python-telegram-bot.
2. WB API for token validation, product ownership, order/pickup/return checks.
3. TON/USDT transfer path for withdrawals.
4. Yandex Cloud primitives: Compute, VPC, Logging.

## 3. Implementation Plan

## Phase 0: Specification Lock

Deliverables:

1. Freeze state machine and transitions.
2. Freeze ledger model and accounting invariants.
3. Freeze contracts between bot handlers and worker jobs.

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
4. OS Login enabled on VMs.
5. Private DB networking with NAT gateway for outbound internet.

Exit criteria:

1. `terraform apply` successful.
2. Bot IG healthy and auto-recovers.
3. DB has private-only interface and is reachable from bot SG.
4. Terraform state is clean (`plan` shows no changes).

Status:

- Completed.

## Phase 2: Backend Foundation

Deliverables:

1. Service layout:
   - `services/bot_api`
   - `services/worker`
   - shared domain modules
2. PostgreSQL migrations and schema.
3. Config, DB session management, structured logging library.
4. Ledger primitives with transactional guarantees.

Exit criteria:

1. Reproducible migrations.
2. Ledger invariants covered by tests.

Status:

- Pending.

## Phase 3: Seller Features

Deliverables:

1. Seller onboarding.
2. Shop creation and deep-link generation.
3. WB token save/validate.
4. Listing creation with ownership checks and collateral checks.
5. Listing activation/pause behavior.

Exit criteria:

1. Listings activate only with valid WB token + full collateral.
2. Token invalidation auto-pauses listing.

Status:

- Pending.

## Phase 4: Buyer Features

Deliverables:

1. Buyer deep-link entry and listing browse.
2. Slot acceptance and reserve lock.
3. 2-hour reservation timeout logic.
4. Order ID submission and validation.
5. Assignment status view for buyer.

Exit criteria:

1. Deterministic reserve/timeout behavior.
2. Duplicate or mismatched order IDs rejected.

Status:

- Pending.

## Phase 5: Workflow Automation (Worker)

Deliverables:

1. Scheduled jobs for:
   - reserve expiry,
   - WB verification,
   - pickup detection,
   - 14-day unlock checks,
   - return checks.
2. Retry/idempotency guards.

Exit criteria:

1. End-to-end transition automation works across restarts.
2. No duplicate unlock/payout events.

Status:

- Pending.

## Phase 6: Finance and Admin Controls

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

## Phase 7: Observability and Reliability

Deliverables:

1. Structured logs with correlation IDs.
2. Logging queries/dashboard for key errors.
3. Alerts/runbooks for worker, WB integration, payout failures.
4. DB backup/restore runbook.

Exit criteria:

1. Critical flows observable and diagnosable.
2. Restore procedure tested at least once.

Status:

- Pending.

## Phase 8: Hardening and Launch

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
2. Start Phase 2 foundation.
3. Implement Phases 3 and 4 for core user value.
4. Implement Phases 5 and 6 for automation and money flows.
5. Complete Phases 7 and 8 before production launch.

## 5. Tracking Policy

On every relevant change:

- update `AGENTS.md` for decisions/state/runbook changes,
- update `PLAN.md` for requirement/phase/status changes,
- ensure both files remain consistent.
