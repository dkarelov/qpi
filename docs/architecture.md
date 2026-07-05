# QPI Architecture: Implemented System Components

Codebase map with decision-bearing annotations. Read this when you need to locate
ownership of a behavior or understand a module boundary; product rules live in
`docs/product/requirements.md`, operating rules in `AGENTS.md`.

## Runtime services

- `services/bot_api`: always-on marketplace bot runtime on VM; long polling is the default Telegram update intake mode.
- `services/daily_report_scrapper`: Cloud Function, hourly WB report sync.
- `services/order_tracker`: Cloud Function, 5-minute assignment lifecycle orchestrator.
- `services/blockchain_checker`: Cloud Function, 5-minute seller collateral top-up matcher and withdrawal payout verifier.
- `services/worker`: minimal placeholder CLI with no critical production ownership.
- `apps/support-bot/*`: companion private-only long-polling Support Topic stack with vendored Python upstream app, local Docker/compose overlay, and dedicated deploy workflow.

## Shared layers

- `libs/domain/*`: transactional domain services (plain SQL).
- `libs/domain/purchase_lifecycle.py`: deep Purchase lifecycle module; owns Purchase state transitions, semantic Cashback/Collateral movement, seller delete settlement for active purchases, and Purchase notification enqueueing while preserving the existing `assignments` table/status strings.
- `libs/domain/purchase_tokens.py`: compact WB Order proof and Review Confirmation token decoders, including legacy no-type review-token compatibility.
- `libs/domain/ledger.py`: generic finance primitive module for accounts, ledger transfers, holds, withdrawals, manual deposits, system provisions, and admin audit records; it does not expose Purchase-specific transition methods.
- `libs/domain/seller_workflow.py`: backend seller activation/unpause facade that performs live WB product checks before mutating listing state.
- `libs/integrations/*`: WB/TonAPI/FX clients.
- `libs/config/settings.py`: runtime settings contracts.
- `libs/logging/setup.py`: YC-compatible structured logging.
- `libs/db/*`: pool and schema tooling.
- `services/bot_api/seller_listing_creation_flow.py`: transport-neutral seller listing creation flow shared by button UX and `/listing_create`; the Telegram runtime maps its effects to Telegram.
- `services/bot_api/seller_marketplace_flow.py`: transport-neutral seller marketplace flow for dashboard, knowledge screens, shop management, announcement management, seller balance, collateral top-up, and transaction history screens; the Telegram runtime bootstraps sellers and applies its effects.
- `services/bot_api/presentation.py`: shared transport-neutral presentation helpers for bot Screens — screen text and title decoration, money/date/status formatting, cashback percent math, withdrawal request/history blocks, buyer listing detail HTML, review-phrase text, and numbered pagination button layout; used by the Telegram runtime and all role flows (tests in `tests/test_presentation.py`).
- `services/bot_api/transport_effects.py`: shared transport-neutral effect vocabulary for role flows; `TelegramWebhookRuntime` is the existing Telegram adapter class name even though polling is the default runtime mode. `SetPrompt.role`, when set, intentionally overrides executor `default_role` for the stored prompt context.
- `services/bot_api/withdrawal_flow.py`: shared transport-neutral seller/buyer withdrawal request creation and cancellation flow; the Telegram runtime supplies role-specific account and TON validation adapters.
- `services/bot_api/buyer_marketplace_flow.py`: transport-neutral buyer marketplace and purchase lifecycle flow for dashboard, knowledge screens, saved shops, shop catalog, announcement detail, buyer balance and withdrawal history screens, reservation, proof/review submission, and purchase cancellation screens.
- `services/bot_api/admin_exceptions_flow.py`: transport-neutral admin exception flow for blocked buyer review confirmations, seller deposit anomalies, manual review verification, deposit attach, and expired invoice cancellation prompts.
- `services/bot_api/telegram_notifications.py`: bot-runtime-only Telegram notification renderer; shared outbox enqueue/claim logic stays in `libs/domain/notifications.py`.
- `services/bot_api/telegram_proxy_request.py`: bot-runtime Telegram Bot API request wrapper that alternates configured HTTP(S) proxies, retries transport/5xx failures, and records proxy-health metrics in Yandex Monitoring.

## Scripts

- `scripts/dev/*`: canonical local reset/test/export wrappers.
- `scripts/deploy/runtime.sh`: canonical bot VM rollout entrypoint.
- `scripts/deploy/function.sh`: canonical code-only Cloud Function rollout entrypoint.
- `scripts/deploy/preflight.sh`: canonical shared deploy preflight entrypoint for runtime, function, and support-bot rollouts.
- `scripts/deploy/schema_remote.sh`: canonical production-schema cleanup/assert/apply entrypoint over the bot-VM bastion.
- `scripts/deploy/qpi_pg_mcp.sh`: canonical read-only production PostgreSQL MCP bootstrap on the bot-VM jump host.
- `scripts/deploy/support_bot.sh`: canonical support-bot image rollout entrypoint.
- `scripts/deploy/private_runner.sh`: canonical on-demand private runner lifecycle entrypoint for CI/deploy jobs.

## Persistence and schema

- PostgreSQL + `psqldef`.
- `schema/schema.sql` is the only schema source of truth.
- Support bot PostgreSQL state lives in the existing qpi database under schema `support_bot`; Redis holds only ephemeral FSM/session state.
