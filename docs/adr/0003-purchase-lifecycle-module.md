# ADR 0003: Purchase Lifecycle Module

## Status

Accepted.

## Context

The marketplace Purchase lifecycle was spread across buyer facades, seller deletion code, order tracking, and Finance ledger helpers. Callers had to know stored assignment status strings, account kinds, ledger event names, hold handling, idempotency-key shapes, and which notifications to enqueue for each transition.

That made `FinanceService` too broad. It mixed generic ledger and withdrawal primitives with Purchase-specific meaning such as reserving slots, releasing cashback, expiring reservations, and unlocking buyer rewards.

## Decision

Add `libs/domain/purchase_lifecycle.py` as the deep module for Purchase lifecycle behavior.

`PurchaseLifecycleService` owns:

- Purchase state transitions while preserving the existing `assignments` table and stored status strings;
- semantic Cashback and Collateral movement for reserve, cancel, expiry, return, delivery expiry, unlock, and seller delete settlement;
- Purchase-related notification enqueueing;
- Purchase review confirmation verification, including admin verification;
- locked internal methods used by seller shop/listing delete so multi-listing shop deletion composes in one transaction.

Add `libs/domain/purchase_tokens.py` for compact WB Order proof and Review Confirmation token decoding. Confirmation tokens stay compact-only, and the transitional legacy no-type review token remains accepted with `wb_product_id` validation against the locked Purchase.

Keep `FinanceService` as the generic primitive module for accounts, transfers, holds, withdrawals, manual deposits, system provisions, and admin audit records. It no longer exposes assignment-specific public methods.

Facade modules keep stable runtime-facing method names where needed. `BuyerService`, `SellerService`, and `OrderTrackerService` delegate Purchase mutations to `PurchaseLifecycleService`; Telegram callback data, prompt names, public refs, DB table names, and stored statuses remain stable.

## Consequences

- New domain code should use Purchase vocabulary and call `PurchaseLifecycleService` for Purchase mutations.
- `FinanceService` callers must not encode Purchase lifecycle state or account movement semantics themselves.
- Tests for transition and money behavior should target the Purchase lifecycle seam; role-flow and runtime tests should stay focused on Telegram effects, copy, callbacks, and prompt behavior.
- Schema migration is intentionally out of scope for this decision. `assignments`, `assignment_id`, and existing status values remain the persisted contract.
