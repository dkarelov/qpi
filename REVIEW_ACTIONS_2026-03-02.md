# Review Actions (2026-03-02)

Scope: actionable fixes from the full codebase review, excluding security deferrals accepted for MVP.

## Included Issues

1. **Order/product correctness gap in reward flow**
- Risk: assignment can progress on WB events matched only by `order_id`, without strict product match.
- Fix plan:
  - enforce product match in `order-tracker` WB event query (`nm_id == listing.wb_product_id`);
  - harden buyer payload validation path to support explicit `wb_product_id` and reject mismatches.

2. **Blockchain scanner cursor can skip unprocessed txs under backlog**
- Risk: cursor can advance even when page budget is exhausted, causing missed operations.
- Fix plan:
  - only advance cursor when shard history scan is complete for the cycle;
  - keep cursor unchanged when scan ends due to page cap.

3. **Duplicate reservation race for same buyer/product**
- Risk: concurrent reserve calls can create duplicate active assignments for the same product.
- Fix plan:
  - add schema-level guard for active `(buyer_user_id, wb_product_id)`;
  - persist `wb_product_id` on assignment creation and map unique violations to domain errors.

4. **Buyer catalog exposes sold-out listings**
- Risk: listings with `available_slots=0` are shown and fail later on reserve.
- Fix plan:
  - filter buyer catalog to `available_slots > 0`.

7. **Global bot error handler does not notify user**
- Risk: domain errors reaching the global handler result in no visible user reaction.
- Fix plan:
  - add safe user-facing fallback reply in global error handler for both domain and unexpected errors.

## Deliberately Excluded (MVP-accepted)

- Point 5: replace token cipher with authenticated encryption/KMS-backed mechanism.
- Point 6: remove insecure runtime secret defaults and enforce strict production-only secret checks.

