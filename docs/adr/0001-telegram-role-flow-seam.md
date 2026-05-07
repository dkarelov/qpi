# Telegram role flow seam

Status: accepted

## Context

The marketplace bot has accumulated buyer, seller, and admin behavior inside the webhook runtime. That made the runtime responsible for python-telegram-bot adaptation, user-facing Russian copy, prompt progression, button layout, domain calls, and domain error mapping at the same time.

The first extraction, seller listing creation, showed the desired direction: role behavior can be tested through transport-neutral results while the runtime stays responsible for executing Telegram operations. The next planned slices are shared withdrawal handling, buyer marketplace behavior, and admin exceptions.

## Decision

Marketplace Telegram behavior is split between role-flow modules and the webhook runtime.

Role-flow modules own:

- user-facing copy;
- prompt progression;
- button descriptors;
- domain error mapping;
- decisions about which Telegram-visible effect should happen next.

The webhook runtime remains the python-telegram-bot adapter. It receives Telegram updates, normalizes them into role-flow inputs, executes shared transport effects, persists prompt state, wires dependencies, and keeps python-telegram-bot types out of role-flow modules.

Shared transport effects are the seam between role-flow modules and the runtime. They describe Telegram-visible intents, prompt state, callback feedback, source-message deletion, and logging without exposing python-telegram-bot types.

## Considered Options

Keep all role behavior in the webhook runtime.

This preserves short-term simplicity but keeps low locality: small UX or domain-flow changes require editing a large adapter file and testing through runtime internals.

Extract pure formatting helpers only.

This reduces line count but keeps prompt progression, domain error mapping, and sequencing in the runtime. The resulting modules would be shallow because deleting them would mostly move string formatting back into callers.

Introduce role-flow modules with a transport-effect seam.

This gives each role-flow module a deeper interface: callers submit semantic inputs and receive transport-neutral effects. The runtime stays as one concrete Telegram adapter.

## Consequences

- Existing callback names and prompt names stay stable unless a product change explicitly requires otherwise.
- Role-flow modules should not import python-telegram-bot types.
- The shared transport-effect vocabulary belongs near the bot runtime, not in domain modules.
- The effect executor can remain runtime-local until multiple role-flow modules exercise the seam.
- Tests should prefer the role-flow interface for role behavior and the runtime adapter tests for Telegram effect execution.
