# QPI Context

`AGENTS.md` remains the project source of truth for product requirements, operating rules, and deployed state.
This file captures architecture-review vocabulary used while designing deeper modules.

## Telegram Role Flow

A Telegram role flow is the role-specific marketplace behavior for one Telegram capability: seller, buyer, or admin.
It owns screen decisions, prompt progression, and calls into domain modules for that role.

The Telegram runtime is only the adapter for python-telegram-bot: it receives updates, normalizes input,
persists per-user session state, executes returned transport effects, and wires dependencies.

## Transport Effect

A transport effect is a role-flow result that describes what the Telegram runtime should do without importing
python-telegram-bot types into the role flow. Examples include sending or editing text, sending a photo,
deleting a sensitive message, answering a callback, setting prompt state, clearing prompt state, and emitting
a log event.

## Seller Listing Creation Flow

Seller listing creation flow is the first seller role-flow slice to extract. It covers listing input parsing,
FX conversion, WB product snapshot lookup, buyer price fallback, buyer-visible title review, prompt progression,
and draft creation for both button-driven UX and the supported `/listing_create` command adapter.

The `/listing_create` command remains a compressed command surface with optional `|| buyer_price_rub ||
display_title` segments. It should reuse the same draft-preparation and draft-finalization behavior as the
button-driven flow instead of carrying a separate implementation.

Seller listing creation flow owns FX refresh and fallback behavior. WB token loading and decryption stay behind
`SellerWorkflowService`, so token crypto does not leak into the role flow. Product-photo presentation is a
transport effect emitted by the flow rather than a runtime-side rendering decision.

The runtime maps Telegram callback names, text messages, and command messages into semantic seller listing
creation inputs such as starting listing creation, submitting listing input, submitting manual buyer price,
keeping the suggested title, editing the suggested title, submitting an edited title, and creating the draft.

Seller listing creation flow owns Russian copy, button layout descriptors, parse mode decisions, and mapping
expected domain errors into user-facing messages. Existing Telegram callback action names should stay stable;
the runtime translates them into semantic inputs so old buttons remain compatible. Extraction should use a
strangler path: move listing creation first, keep existing runtime helpers while needed, then delete helper
methods once the flow owns them.
