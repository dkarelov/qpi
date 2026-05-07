# QPI Context

QPI is a Telegram marketplace where WB sellers fund buyer cashback in USDT for completing a target purchase flow.
`AGENTS.md` remains the source of truth for requirements, operating rules, and deployed state.

## Language

**Telegram Account**:
The Telegram identity through which a person uses the marketplace.
_Avoid_: account, user account

**Capability**:
A marketplace permission held by a **Telegram Account**, such as seller, buyer, or admin.
_Avoid_: role when implying a person can have only one

**Seller**:
A **Capability** that manages shops, announcements, collateral, and seller withdrawals.

**Buyer**:
A **Capability** that saves shops, reserves purchases, submits proof, and withdraws cashback.

**Admin**:
A **Capability** that decides withdrawal requests and finance exceptions.

**Shop**:
A seller-owned buyer-facing storefront that groups announcements and has a deep link.

**Announcement**:
A seller-created buyer-facing offer for one WB product.
_Avoid_: listing, task, ad

**Purchase**:
A buyer reservation or work item tied to one **Announcement**.
_Avoid_: task, assignment, order

**WB Order**:
The Wildberries order identifier and event stream used to verify a **Purchase**.
_Avoid_: purchase

**Review Confirmation**:
Buyer proof that the required review was posted for a **Purchase**.

**Cashback**:
The buyer reward funded by seller collateral, shown mostly in RUB and settled in USDT.

**Collateral**:
Seller USDT reserved to fund cashback for active announcements.

**Withdrawal Request**:
A request to move available buyer or seller balance to an external TON address.
_Avoid_: payout when referring to the pending request

**Seller Deposit Invoice**:
A time-limited seller top-up request with an exact USDT amount for chain matching.
_Avoid_: bill, payment request

**Admin Exception**:
A manual queue item requiring an admin decision, including deposit anomalies and blocked review confirmations.

**Support Reference**:
A short public identifier used for support conversations, such as `S4`, `L8`, `P31`, `W7`, `D12`, or `TX5`.

## Relationships

- A **Telegram Account** can hold one or more **Capabilities**.
- A **Seller** owns zero or more **Shops**.
- A **Shop** contains zero or more **Announcements**.
- A **Buyer** can save zero or more **Shops**.
- A **Buyer** can create a **Purchase** from an active **Announcement**.
- A **Purchase** belongs to exactly one **Buyer** and exactly one **Announcement**.
- A **Purchase** may have one **WB Order** and one **Review Confirmation**.
- **Cashback** for a **Purchase** is funded by **Collateral** and can become buyer available balance.
- A **Withdrawal Request** belongs to exactly one requester **Capability**: buyer or seller.
- A **Seller Deposit Invoice** belongs to exactly one **Seller**.
- An **Admin Exception** references a **Purchase**, **Seller Deposit Invoice**, or incoming chain transaction.
- A **Support Reference** points to one marketplace entity but does not replace the entity itself.

## Example Dialogue

> **Dev:** "Can a Telegram account be both a **Seller** and a **Buyer**?"
> **Domain expert:** "Yes. Treat seller and buyer as **Capabilities**, not mutually exclusive identities."
>
> **Dev:** "When a buyer taps `Купить`, do we create a WB order?"
> **Domain expert:** "No. We create a **Purchase** first; the **WB Order** appears only after the buyer submits proof."

## Flagged Ambiguities

- "account" is ambiguous: use **Telegram Account** for identity, ledger account for internal balances, and TON address for external wallets.
- "listing" is code vocabulary for **Announcement**; use **Announcement** in product/domain discussion.
- "assignment" and "task" are code vocabulary for **Purchase**; use **Purchase** in product/domain discussion.
- "order" means **WB Order** unless explicitly qualified; it is not the same as a **Purchase**.
