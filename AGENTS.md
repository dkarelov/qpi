# QPI AGENTS

Last updated: 2026-07-05 UTC

## 0. Completion Gate

This section is intentionally first because it is a delivery gate, not a soft preference.

- For any code, schema, infra, workflow, or requirement change, local implementation and local tests are not enough to call the task complete.
- Default completion sequence:
  - finish the implementation,
  - run required local validation,
  - commit the finished work,
  - push the current branch,
  - inspect the triggered GitHub workflows,
  - wait for them to reach a terminal state,
  - if workflows fail, continue debugging or report the exact blocker; do not present the task as complete.
- Do not stop after local validation unless the operator explicitly says to stop before commit/push/workflow verification.
- If a push succeeds but post-push workflows fail, the task remains incomplete.
- Never report success while the pushed commit is still red or while its required workflows are still running.

## 1. Documentation Policy

`AGENTS.md` is the project source of truth for product and development
knowledge:

- current product requirements,
- implemented decisions,
- operating rules,
- local test runbooks and safeguards.

DevOps knowledge (infrastructure state, CI/CD architecture, deploy/runner/
Terraform runbooks, operational gotchas) lives in `docs/ops/devops.md` and is
loaded only for devops-specific tasks (see Section 6).

Current repo scope:

- qpi marketplace runtime (Python + PostgreSQL),
- companion support-bot runtime (Python + PostgreSQL schema + Redis) under `apps/support-bot`,
- shared Terraform, runner, and deploy conventions.

Documentation rules:

- Keep `AGENTS.md` and `docs/ops/devops.md` aligned with actual code and Terraform state; a change that alters deploy/infra behavior updates `docs/ops/devops.md` in the same commit.
- Record only current behavior and active decisions.
- Do not keep phase-by-phase history or superseded evolution notes here.
- Keep details that affect delivery/operations; omit minor refactors that are obvious from git history and code.
- During active development, the default completion path after implementation is:
  - commit the finished work,
  - push the current branch,
  - verify the triggered workflows pass,
  - confirm the resulting state is working as expected.
- Only skip commit/push/workflow validation when the operator explicitly asks to stop before that stage.

Glossary for Telegram UX:

- `Объявление` = seller-created buyer-facing offer for one WB product.
- `Покупка` = buyer reservation/work item tied to one announcement.
- `Активно` = user-facing wording for active/open availability or status.
- `Support Topic` = canonical support conversation unit: one Telegram forum topic per Telegram Account in the support supergroup.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `dkarelov/qpi`. See `docs/agents/issue-tracker.md`.

### Triage labels

Agent triage uses the canonical `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix` labels. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo with root `CONTEXT.md` and root `docs/adr/`. See `docs/agents/domain.md`.

## 2. Product Scope (Current MVP)

Goal:

- Telegram marketplace bot where WB sellers fund buyer cashback in USDT for completing the target purchase flow.

Actors:

- Seller: manages shops and listings, funds collateral, monitors status.
- Buyer: takes tasks, submits verification token, withdraws unlocked cashback.
- Admin: handles withdrawals and finance exceptions.
- One Telegram account can hold multiple capabilities at once (`seller`, `buyer`, `admin`).

In scope:

- Single Telegram marketplace bot (`python-telegram-bot`) with role-based UX.
- Russian UX text.
- WB token validation and report-based order lifecycle tracking.
- Ledger in USDT.
- Scheduled CF automation for WB report sync, order tracking, and collateral deposit matching.
- Manual admin override paths for exceptions.

Out of scope (MVP):

- Disputes.
- Advanced custody model (multisig/HSM/KMS-backed wallet signing).
- Full automatic reconciliation for all inbound/outbound wallet flows.

Companion support-bot (current decision boundary):

- separate operational surface from the marketplace bot,
- isolated runtime and VM; persistent state is in the existing PostgreSQL cluster under the `support_bot` schema,
- Telegram-only in V1,
- Russian UX text,
- forum-topic Support Topic model: one Support Topic per Telegram account in the support supergroup,
- no Signal, web chat, or backup automation in V1,
- Redis is ephemeral support-bot runtime state and is capped separately from PostgreSQL state,
- Telegram Bot API egress uses the first URL from `TELEGRAM_API_PROXY_URLS`,
- end-user support-bot private chat UX is Russian-only and does not expose a language-selection step,
- marketplace buyer/seller screens may deep-link into the support bot when `SUPPORT_BOT_USERNAME` is configured,
- support-bot `/start` payload parsing, Support Topic title metadata, Russian delivery semantics, media/albums, service-backed lifecycle controls, optional capabilities, and Python deploy workflow are the active runtime baseline.

## 3. Implemented System Components

Runtime services:

- `services/bot_api`: always-on marketplace bot runtime on VM; long polling is the default Telegram update intake mode.
- `services/daily_report_scrapper`: Cloud Function, hourly WB report sync.
- `services/order_tracker`: Cloud Function, 5-minute assignment lifecycle orchestrator.
- `services/blockchain_checker`: Cloud Function, 5-minute seller collateral top-up matcher and withdrawal payout verifier.
- `services/worker`: placeholder runtime (legacy/no critical ownership).
- `apps/support-bot/*`: companion private-only long-polling Support Topic stack with vendored Python upstream app, local Docker/compose overlay, and dedicated deploy workflow.

Shared layers:

- `libs/domain/*`: transactional domain services (plain SQL).
- `libs/domain/purchase_lifecycle.py`: deep Purchase lifecycle module; owns Purchase state transitions, semantic Cashback/Collateral movement, seller delete settlement for active purchases, and Purchase notification enqueueing while preserving the existing `assignments` table/status strings.
- `libs/domain/purchase_tokens.py`: compact WB Order proof and Review Confirmation token decoders, including legacy no-type review-token compatibility.
- `libs/domain/ledger.py`: generic finance primitive module for accounts, ledger transfers, holds, withdrawals, manual deposits, system provisions, and admin audit records; it does not expose Purchase-specific transition methods.
- `libs/domain/seller_workflow.py`: backend seller activation/unpause facade that performs live WB product checks before mutating listing state.
- `libs/integrations/*`: WB/TonAPI/FX clients.
- `libs/config/settings.py`: runtime settings contracts.
- `libs/logging/setup.py`: YC-compatible structured logging.
- `services/bot_api/seller_listing_creation_flow.py`: transport-neutral seller listing creation flow shared by button UX and `/listing_create`; the Telegram runtime maps its effects to Telegram.
- `services/bot_api/presentation.py`: shared transport-neutral presentation helpers for bot Screens — screen text and title decoration, money/date/status formatting, cashback percent math, withdrawal request/history blocks, buyer listing detail HTML, review-phrase text, and numbered pagination button layout; used by the Telegram runtime and all role flows (tests in `tests/test_presentation.py`).
- `services/bot_api/transport_effects.py`: shared transport-neutral effect vocabulary for role flows; `TelegramWebhookRuntime` remains the named Telegram adapter that executes those effects in polling or explicit webhook mode. `SetPrompt.role`, when set, intentionally overrides executor `default_role` for the stored prompt context.
- `services/bot_api/withdrawal_flow.py`: shared transport-neutral seller/buyer withdrawal request creation and cancellation flow; the Telegram runtime supplies role-specific account and TON validation adapters.
- `services/bot_api/buyer_marketplace_flow.py`: transport-neutral buyer marketplace and purchase lifecycle flow for dashboard, knowledge screens, saved shops, shop catalog, announcement detail, buyer balance and withdrawal history screens, reservation, proof/review submission, and purchase cancellation screens.
- `services/bot_api/admin_exceptions_flow.py`: transport-neutral admin exception flow for blocked buyer review confirmations, seller deposit anomalies, manual review verification, deposit attach, and expired invoice cancellation prompts.
- `services/bot_api/telegram_notifications.py`: bot-runtime-only Telegram notification renderer; shared outbox enqueue/claim logic stays in `libs/domain/notifications.py`.
- `services/bot_api/telegram_proxy_request.py`: bot-runtime Telegram Bot API request wrapper that alternates configured HTTP(S) proxies, retries transport/5xx failures, and records proxy-health metrics in Yandex Monitoring.
- `libs/db/*`: pool and schema tooling.
- `scripts/dev/*`: canonical local reset/test/export wrappers.
- `scripts/deploy/runtime.sh`: canonical bot VM rollout entrypoint.
- `scripts/deploy/function.sh`: canonical code-only Cloud Function rollout entrypoint.
- `scripts/deploy/preflight.sh`: canonical shared deploy preflight entrypoint for runtime, function, and support-bot rollouts.
- `scripts/deploy/schema_remote.sh`: canonical production-schema cleanup/assert/apply entrypoint over the bot-VM bastion.
- `scripts/deploy/qpi_pg_mcp.sh`: canonical read-only production PostgreSQL MCP bootstrap on the bot-VM jump host.
- `scripts/deploy/support_bot.sh`: canonical support-bot image rollout entrypoint.
- `scripts/deploy/private_runner.sh`: canonical on-demand private runner lifecycle entrypoint for CI/deploy jobs.

Persistence and schema:

- PostgreSQL + `psqldef`.
- `schema/schema.sql` is the only schema source of truth.
- Support bot PostgreSQL state lives in the existing qpi database under schema `support_bot`; Redis holds only ephemeral FSM/session state.

## 4. Functional Requirements and Rules

### 4.1 Seller rules

- Seller can create multiple shops.
- Shop names must be unique per seller (case-insensitive).
- Shop name is visible to buyers; seller is warned to use a neutral, understandable title.
- Shop rename regenerates slug/deeplink; seller is warned that old link stops working.
- Shop/listing deletion is soft-delete only.
- Deletion is not blocked by active entities; warning is mandatory before confirmation.
- On confirmed delete:
  - assignment-linked reserved funds go to buyers irreversibly,
  - unassigned collateral returns to seller.
- Delete-time unassigned collateral is computed after deducting active assignment rewards and rewards already paid for that listing; already-paid buyer cashback is never refundable seller collateral.
- Listing creation input:
  - `wb_product_id`,
  - cashback in RUB,
  - slot count,
  - search phrase,
  - optional review phrase pool (`0..10` phrases).
- Seller listing-create input is a single comma-separated message:
  - `wb_product_id, cashback_rub, slot_count, search_phrase, review_phrase_1, ... , review_phrase_10`.
- Seller slash-command adapter `/listing_create` is a supported runtime surface, not a legacy fallback:
  - it must stay aligned with the same listing-create contract as the button-driven UX,
  - it accepts the same comma-separated listing payload after `<shop_id>`,
  - when command mode needs to compress the interactive fallback steps into one message, it appends optional segments as `|| buyer_price_rub || display_title`.
- Listing review phrase pool rules:
  - blank phrase cells are ignored,
  - at most 10 non-empty phrases are stored,
  - buyer review prompts later receive up to 2 phrases chosen randomly from that pool.
- Listing draft creation fetches WB metadata live by `wb_product_id` using the seller's WB token and stores:
  - buyer-visible `display_title`,
  - WB source title,
  - WB subject,
  - WB brand,
  - WB vendor code,
  - WB description,
  - WB photo URL (`c516x688`),
  - WB tech sizes,
  - WB characteristics,
  - buyer price in RUB.
- Buyer price source:
  - primary: derived from `GET https://statistics-api.wildberries.ru/api/v1/supplier/orders` over the last 30 days,
  - fallback: manual seller input when no historical orders exist for the product.
- Seller confirms or edits the buyer-visible title before the draft is saved.
- Seller listing confirmation preview shows the product photo when WB returned one.
- Each listing has a buyer product deeplink shaped as `listing_<listing_id>`; sellers share this product link with buyers so the buyer lands on the exact announcement.
- Seller cannot edit an existing announcement after creation; if parameters must change, the seller creates a new announcement and deletes the old one.
- Cashback is converted once to fixed `reward_usdt` at creation.
- Listing collateral requirement: `reward_usdt * slot_count * 1.01`.
- Listing activation/unpause requires:
  - valid WB token,
  - sufficient seller funds,
  - successful live WB metadata read for the stored `wb_product_id`.
- Seller can withdraw only `seller_available`; `seller_collateral` and active collateral holds are never withdrawable.
- Seller can have at most one active withdrawal request at a time.
- Seller can cancel their own withdrawal request while it is still pending admin action and then create a new one.
- Seller withdrawal address must pass TonAPI parse validation for TON mainnet before the request is created.

### 4.2 Buyer rules

- Buyer can open an exact announcement by product deeplink `listing_<listing_id>`; legacy shop deeplinks `shop_<slug>` and saved shops still open the shop catalog.
- Product deeplinks reopen the product card for buyers who already have an active or completed purchase for the same WB item, hide the reserve action, and explain the repeat-purchase block instead of showing a generic unavailable state; the `Покупки` shortcut is shown only when the matching purchase is visible there.
- Buyer can reserve slot only on active listings.
- Buyer-facing primary CTA for an active listing is `Купить`.
- Buyer-facing listing screens show buyer-visible title, WB subject, description, photo, sizes, characteristics, cashback in RUB with approximate percent, and `Цена` in RUB.
- WB product photos may be stored as WB `.webp` URLs; Telegram photo delivery uses bounded in-memory upload/conversion fallback for trusted WB image CDN hosts only, revalidates redirects/final URLs against that allowlist, and must not rely only on Telegram fetching the URL or create a disk cache.
- Buyer-facing listing screens/cards must not expose WB article (`Артикул WB` / `Артикул ВБ`), WB brand, or WB source title.
- Buyer receives setup token (base64 JSON array):
  - `[1, task_uuid, search_phrase, wb_product_id, 1, wb_brand_name]`, where `task_uuid` is the immutable assignment UUID and `wb_brand_name` is an empty string when unavailable.
- Buyer submits verification token (base64 JSON array):
  - `[task_uuid, order_id, ordered_at]`, where `ordered_at` is an ISO datetime; timezone-bearing values are accepted and normalized to UTC.
- Buyer can paste a valid verification token or review confirmation token directly while in buyer context without first opening the token-input prompt; the runtime resolves the purchase by `task_uuid`, applies the same validation/write path, and deletes the sensitive token message when possible.
- Verification token `ordered_at` values more than 15 minutes in the future are rejected.
- After pickup, buyer receives review setup token (base64 JSON array):
  - `[2, task_uuid, wb_product_id, review_phrase_1?, review_phrase_2?]`, where phrases are omitted when the seller did not provide them.
- Buyer submits review confirmation token (base64 JSON array):
  - `[task_uuid, reviewed_at, review_score, review_text]`, where `reviewed_at` is an ISO datetime; timezone-bearing values are accepted and normalized to UTC.
- Buyer review UX is instruction-first:
  - review-required notifications and purchase rows use `✍️ Оставить отзыв`,
  - that action opens review instructions before the paste prompt,
  - instructions tell the buyer to copy the setup token into Qpilka, publish a 5-star WB review with required phrases, then return with the extension-issued confirmation token,
  - the paste prompt button is `✅ У меня есть токен подтверждения`,
  - the paste prompt title is `Токен-подтверждение отзыва`.
- Order and review confirmation tokens are compact-only; token type and `wb_product_id` are derived from the locked assignment and are not accepted in confirmation payloads.
- Transitional compatibility: review confirmation also accepts already-generated no-type legacy tokens shaped as `[task_uuid, wb_product_id, reviewed_at, review_score, review_text]`; the embedded `wb_product_id` must match the locked assignment.
- Verification token must be submitted within 4 hours of reservation.
- `order_id` is globally unique (`1 order_id = 1 slot`).
- Review confirmation is mandatory after pickup. Without it, cashback stays frozen even after the unlock timer has passed.
- Automatic review verification requires `review_score = 5` and presence of every non-empty required phrase selected for that assignment.
- If automatic review verification fails, the review is stored for manual review, the assignment remains `picked_up_wait_review`, and cashback stays blocked until the buyer corrects the review or an admin manually verifies the token.
- Verification/review tokens with mismatched `task_uuid`, wrong WB product, wrong buyer ownership, or wrong token type must be rejected without state changes.
- Buyer can cancel purchase only while the assignment is still `reserved`.
- Buyer cancellation is a distinct terminal lifecycle outcome (`buyer_cancelled`), separate from timeout expiry.
- Validation must happen as early as possible in buyer flows and still be rechecked at final write/transfer time.
- Buyer can have at most one active withdrawal request at a time.
- Buyer can cancel their own withdrawal request while it is still pending admin action and then create a new one.
- Buyer withdrawal address must pass TonAPI parse validation for TON mainnet before the request is created.
- One buyer cannot repeatedly buy the same target item:
  - duplicate reserve attempts are blocked,
  - already-bought item is not treated as a new available task.
- Buyer cannot remove a saved shop while they still have an unfinished purchase in that shop.

### 4.3 Assignment lifecycle rules

In-progress states:

- `reserved`
- `order_verified`
- `picked_up_wait_review`
- `picked_up_wait_unlock`

Completed visible state:

- `withdraw_sent`

Terminal/error states:

- `expired_2h`
- `buyer_cancelled`
- `wb_invalid`
- `returned_within_14d`
- `delivery_expired`

Transitions:

- `reserved -> expired_2h` after 4h without valid verification token (legacy status code name retained).
- `reserved -> buyer_cancelled` when the buyer explicitly cancels before submitting a verification token.
- Valid verification token transitions to `order_verified`.
- WB event `Продажа` transitions to:
  - `picked_up_wait_review` and sets unlock time `pickup + 15d` when review confirmation is required for that assignment,
  - `picked_up_wait_unlock` and sets unlock time `pickup + 15d` for legacy/no-review assignments.
- Valid review confirmation token transitions `picked_up_wait_review -> picked_up_wait_unlock`.
- WB event `Возврат` within unlock window transitions to `returned_within_14d`.
- `order_verified -> delivery_expired` after 60 days without pickup.
- Delivery expiry uses server-side submission time (`assignment.order_submitted_at`) rather than buyer-provided `ordered_at`.
- Unlock timer credits buyer balance and transitions assignment to `withdraw_sent` (`Выплачен`).

### 4.4 Admin and finance rules

- Admin operations are Telegram-driven and auditable.
- Withdrawals use one shared requester model for buyers and sellers and require admin decision path:
  - open request,
  - reject with reason, or
  - enter tx hash for a completed transfer.
- Withdrawal completion can be admin-driven or auto-verified:
  - admin enters tx hash only after sending funds,
  - bot verifies the tx hash on-chain against the configured TON USDT payout wallet, requester address, and exact amount,
  - only a verified tx completes the request,
  - failed/missing tx verification leaves the request pending for retry.
- The blockchain checker also scans recent outgoing TON USDT transfers from the configured payout wallet and auto-completes a pending withdrawal only when exactly one visible, deduped history operation heuristically matches exactly one request:
  - payout wallet source,
  - requester payout address after TonAPI address parsing,
  - exact 6-decimal USDT amount,
  - transfer time not earlier than request creation,
  - tx hash not already recorded on another payout.
- Duplicate TonAPI page-overlap entries by the same tx hash are treated as one operation; conflicting duplicate tx hashes, ambiguous matches, missing matches, unverifiable matches, and payout scans that hit the configured page cap stay pending for manual admin action and emit warnings where operational follow-up is needed.
- Every new buyer or seller withdrawal request sends an admin push notification with requester role, Telegram identity, amount, and request number.
- Manual deposit is supported for exception handling/bonuses/corrections.
- Manual deposit input supports role aliases:
  - `seller` maps to `seller_available`,
  - `buyer` maps to `buyer_available`.
- External reference for manual deposit is mandatory audit metadata and can be either:
  - free-form reason/comment,
  - tx reference (e.g. `tx:...`).
- `system_payout` balance provisioning remains an accepted implementation shortcut for externally funded credits, but every such top-up must create an immutable audit record in `system_balance_provisions`.
- Admin `⚠️ Исключения` now covers both deposit anomalies and blocked buyer review confirmations; admins can manually verify a matching review-confirmation token for `P<assignment_id>` and audibly move the assignment to `picked_up_wait_unlock`.

### 4.5 Seller top-up auto-confirmation rules (blockchain checker)

- Matching model: amount suffix on shard address.
- Base amount formula:
  - `base_amount = ceil(request_amount_usdt * 10) / 10`.
- Expected amount formula:
  - `expected_amount = base_amount + suffix / 10000`.
- Suffix space: `001..999`.
- Active invoice uniqueness: one active suffix per `(shard_id, suffix)`.
- Invoice TTL: 24h.
- Match rule:
  - incoming amount must be `>= expected_amount`, on-time, and not earlier than the invoice creation timestamp.
- Overpayment:
  - full received amount is credited.
- Partial/late payments:
  - route to `manual_review` for admin action.
- MVP shard strategy:
  - one shard address is enabled by default.

### 4.6 WB token and report rules

- Single seller token is used for both:
  - `statistics-api`,
  - `content-api`.
- Seller UX instructs the operator to create a Basic WB token in read-only mode with categories:
  - `Контент`,
  - `Статистика`,
  - `Вопросы и отзывы`.
- WB token initial validation endpoints:
  - `GET https://statistics-api.wildberries.ru/ping`.
  - `GET https://content-api.wildberries.ru/ping`.
- Invalid ping result => token is not persisted.
- Ping rate limit policy: `3 requests / 30s` per process.
- `daily-report-scrapper` invalidates token on WB `401` where message contains:
  - `token expired` => shop token status becomes `expired`,
  - `withdrawn` => shop token status becomes `invalid`,
  - any other `401` auth failure (for example `unauthorized`) => shop token status becomes `invalid`.
- Token invalidation auto-pauses active listings via explicit application transaction (no PG trigger).
- Order tracking matches buyer-submitted `order_id` against WB report identifiers in both forms:
  - exact WB `srid` / stored `wb_srid`,
  - WB `order_uid` and the plain UID segment embedded inside prefixed `srid` values such as `ebu.<uid>.7.0`.
- WB report rows are derived cache and are scoped by `shop_id`; lifecycle matching must require the report row to belong to the same shop as the listing.
- If the report cache schema changes from unscoped to scoped rows, old unscoped cache rows are purged and refilled by the hourly scrapper.

### 4.7 Telegram UX rules (must be preserved)

- Every button press must produce visible feedback:
  - message edit, new reply, or alert.
- Silent no-op callback behavior is forbidden.
- Menus are tree-structured (no flat action panel).
- Dashboard and queue navigation buttons show entity counts when the count is already loaded for that screen:
  - seller dashboard: announcements and shops,
  - buyer dashboard: saved shops and purchases,
  - admin dashboard/sections: pending withdrawals and exception queues.
- Count suffix format is `· n` (for example `Магазины · 2`) across all roles.
- Telegram inline keyboard labels are plain text; count suffixes must not rely on HTML/Markdown formatting.
- Callback-driven navigation is immutable/linear:
  - button presses retire the old inline keyboard,
  - the bot sends a new screen message instead of editing the previous one.
- Standard screen layout:
  - title,
  - optional italic subtitle immediately below the title when it adds useful guidance, context, state, or risk,
  - main content blocks separated by empty lines,
  - optional italic note at the bottom only when it adds non-obvious next steps or issue guidance.
- Buyer screens omit generic subtitles such as "choose an action below" or "check the list below"; buyer empty states and blockers belong in the main body text.
- Button labels include emoji/icon prefix.
- Marketplace support entry uses Telegram URL buttons into the companion support-bot on the seller main dashboard and inside the buyer `📘 Инструкция` screen; if `SUPPORT_BOT_USERNAME` is unset, those buttons are hidden rather than degraded into broken links.
- Buyer support must stay deeper than the first dashboard screen: do not reintroduce a support shortcut on the buyer main cabinet when the intent is to route confused users through manuals/FAQ first.
- Each role opens with dashboard + section navigation.
- Seller/buyer dashboards include `📘 Инструкция`; section/detail screens use role-specific `📘 Про ...` knowledge-base buttons instead of support shortcuts.
- Public support references are short prefixed ids based on existing numeric primary keys:
  - shop: `S<shop_id>`,
  - listing: `L<listing_id>`,
  - purchase/assignment: `P<assignment_id>`,
  - withdrawal: `W<withdrawal_request_id>`,
  - seller deposit invoice: `D<deposit_intent_id>`,
  - admin incoming chain tx: `TX<chain_tx_id>`.
- Shop title, shop slug, WB article, order number, and tx hash remain useful secondary references, but titles/slugs are not the primary support identifier because titles are ambiguous and the slug changes on rename.
- Buyer/seller entity refs are shown subtly in screen or block titles (for example `Магазин ... · S4`) instead of separate `Код ...` body rows.
- Seller UX:
  - `Объявления`, `Магазины`, `Баланс` are top sections,
  - seller dashboard order counters use the same buckets as buyer dashboard: `ожидают заказа`, `заказаны`, `выкуплены`,
  - shop actions are nested: list -> shop card -> actions.
- Seller announcement list uses numbered, paginated navigation:
  - one page shows up to 10 announcements,
  - each announcement is opened by a number button,
  - action buttons such as edit/pause/delete live inside the announcement card, not in the list.
- Seller announcement card is streamlined:
  - title starts with green/red activity indicator,
  - top section shows only WB article, cashback, search phrase, plan/in-progress, product link, collateral, and activity status,
  - the rest of the WB data lives inside collapsed `Параметры`, `Описание`, and `Характеристики` sections,
  - if collateral is insufficient, the note explains that balance top-up is required before activation.
- Seller balance screen shows `Свободно для новых объявлений`, `Уже выделено под объявления`, and `В процессе вывода`; `Всего` is not shown there, and activation shortfall is shown only when funds are insufficient.
- Seller shop/listing cards, seller top-up invoices/history, and seller withdrawal blocks keep copyable public refs in their titles/headers.
- Seller balance screen offers:
  - `➕ Пополнить`,
  - `💸 Вывести все доступное`,
  - `✍️ Указать сумму вручную`,
  - `🧾 Транзакции`,
  - `↩️ Назад`.
- If the seller already has an active withdrawal request, new withdrawal actions are hidden and the screen shows that request plus `🚫 Отменить заявку`.
- Seller top-up amount entry screen also includes `Как перевести?`, which opens the same transfer guidance before invoice creation.
- Seller top-up invoice screen:
  - shows the TON USDT address in copy-friendly monospace,
  - shows the exact `USDT` transfer amount in copy-friendly monospace,
  - includes two wallet actions:
    - Telegram Wallet home opener,
    - generic TON jetton transfer link for other wallets,
  - includes `Как перевести?`, which opens a separate guidance screen with Wallet/P2P help links and TON withdrawal steps,
  - keeps the raw address and amount visible as fallback if wallet opening does not work.
- `Транзакции продавца` is a unified paginated seller balance history for both `Пополнение` and `Вывод`, ordered by creation time descending and showing timestamps, statuses, notes/comments, and tx hash when present.
- Transaction/history screens:
  - use representative `Транзакции ...` titles,
  - use `<` / `>` pagination when needed,
  - separate transaction blocks with empty lines,
  - use color indicators for statuses.
- Buyer UX:
  - shops/purchases/balance sections,
  - buyer section title is `Покупки`, not `Задания`,
  - active listing CTA uses `Купить`,
  - shops section uses a numbered, paginated saved-shop list with number buttons, no `Открыть последний магазин`, and no `Открыть магазин по коду` button,
  - each saved shop row shows a red/green circle and `(объявлений: N)`, where green means at least one active buyer-visible listing for that buyer and red means none,
  - buyer shop catalog uses a numbered, paginated listing list; number buttons open the detail card, and the catalog itself does not show direct `Просмотр` / `Купить` buttons,
  - buyer can remove a shop only from their own saved-shop list, and removal is blocked while that shop has an unfinished buyer purchase,
  - if a shop has no other buyer-visible listings but the buyer already has an active purchase there, the shop screen shows a `Покупки` shortcut instead of a dead end,
  - purchase list uses store title (not slug), shows in-progress and paid purchases, and shows fields in order: `Товар`, `Магазин`, `Кэшбэк`, optional `Номер заказа`, `Статус`,
  - buyer purchase/withdraw blocks and concrete shop/listing screens keep copyable public refs in titles/headers instead of separate `Код ...` lines,
  - purchase status line includes color markers: red for `Ожидает заказа`, yellow for `Заказан` / `Нужно оставить отзыв`, green for `Выкуплен` / `Выплачен`,
  - dashboard purchase counters are grouped as `ожидают заказа`, `заказаны`, `выкуплены`,
  - `Нужно оставить отзыв` stays in the yellow `заказаны` bucket until review confirmation is accepted,
  - buyer dashboard `Баланс` equals withdrawable amount only and is shown as approximate RUB,
  - buyer non-withdrawal screens show cashback/balance primarily as approximate RUB; exact USDT remains visible on withdrawal-specific screens and prompts,
  - buyer non-withdrawal notifications follow the same rule as screens: approximate RUB only; exact USDT is reserved for withdrawal-specific buyer flows,
  - review-required pickup notifications use `Оставить отзыв` as the CTA and open the review setup instructions before token submission,
  - buyer balance screen shows only `Доступно для вывода` and `В процессе вывода`,
  - if the buyer already has an active withdrawal request, new withdrawal actions are hidden and the screen shows that request plus a cancel action,
  - buyer withdrawal history is full paginated history with `<` / `>` navigation, timestamps, comments, and tx hash when available,
  - irrelevant actions must be hidden when they cannot be used in the current state (for example withdrawal buttons when withdrawable balance is zero),
  - purchase flow contains explicit order-token submit, review-token submit, and cancel-purchase actions when relevant.
- Seller notifications:
  - pickup notification can indicate that buyer review is still pending,
  - after review confirmation the seller receives a dedicated notification containing the confirmed rating, review text, and confirmation time.
- All user-facing timestamps are rendered in `МСК` (`Europe/Moscow`).
- Admin UX:
  - `Выводы`, `Депозиты`, `Исключения` sections.
- Admin withdrawals section contains pending/actionable requests and processed-history access for both buyers and sellers, with requester role shown in queue and detail views.
- Sensitive inputs (tokens, payloads) are deleted when possible.

### 4.8 Money, precision, and FX rules

- Ledger currency: USDT.
- UI summary format for mixed display:
  - `$USDT (~RUB ₽)`.
- Buyer-facing marketplace summaries outside withdrawal-specific flows prefer approximate RUB (`~RUB ₽`) while keeping USDT as the underlying ledger currency.
- Buyer-facing notifications follow the same rule: non-withdrawal notifications show approximate RUB, while withdrawal-specific notifications may use exact USDT.
- Rounding:
  - USDT summary: 1 decimal,
  - USDT precise operations: 6 decimals,
  - RUB helper: integer rounded.
- FX helper source for `USDT/RUB`:
  - read cached value from PostgreSQL `fx_rates`,
  - if stale (> `FX_RATE_TTL_SECONDS`, default 900), refresh on demand from CoinGecko,
  - refresh is protected by PostgreSQL advisory lock.

## 5. Technical Constraints and Invariants

- Marketplace services remain Python-only. The companion support-bot runtime is also Python and isolated under `apps/support-bot`.
- Marketplace dependency and environment management are `uv`-based; `.venv` remains the runtime path, but `uv.lock` is the source of truth.
- Support-bot dependency management is nested `uv` under `apps/support-bot/upstream`, with Python 3.14 as the qpi target version.
- `requirements.txt` is generated from `uv.lock` for Cloud Function/Terraform compatibility and is never hand-edited.
- DB access: `psycopg3` + plain SQL only (no ORM).
- For qpi read-only production inspection, use the qpi-specific `qpi-pg-prod` MCP server after it is installed with `scripts/deploy/qpi_pg_mcp.sh` and registered locally with `scripts/dev/qpi_pg_mcp_codex.sh`.
- `qpi-pg-prod` is DBHub launched on demand through SSH stdio on the bot VM jump host. It must not expose HTTP, open a public port, or make PostgreSQL directly reachable from the workstation.
- `qpi-pg-prod` uses the dedicated PostgreSQL role `qpi_mcp_readonly`; DBHub `readonly=true` is only a safety net, not the security boundary.
- Do not use the globally named `pg-prod` MCP for this repo: it is connected to another PostgreSQL database, and its results are invalid for qpi diagnostics, SQL validation, production evidence, and incident investigation.
- For qpi schema changes, production writes, or incident repairs, use the repo-documented `psql`, SSH/bastion, `scripts/deploy/schema_remote.sh`, and CI/private-runner validation paths, not MCP.
- Schema changes only through `schema/schema.sql` + `psqldef`.
- Infrastructure mutations are Terraform-only from `infra/`.
- Marketplace bot runtime uses Telegram long polling by default through the configured outbound Telegram proxies. Webhook mode remains available only as an explicit fallback with `TELEGRAM_UPDATE_MODE=webhook` and valid webhook settings.
- Companion support-bot runtime uses long polling and remains private-only.
- Seller and buyer slash-command adapters (`services/bot_api/seller_handlers.py`, `services/bot_api/buyer_handlers.py`, in-chat command dispatch, and `--seller-command` / `--buyer-command`) are supported interfaces, not legacy-only tooling; changes to shared bot flows must update these adapters in the same change whenever the operation remains available by command.
- `SUPPORT_BOT_USERNAME` is an optional marketplace bot runtime env var; when set, seller/buyer screens can build deep links into the support-bot using the public ref contract above.
- Expected load target: ~100 concurrent users.


## 6. DevOps Knowledge Base

Infrastructure state, CI/CD architecture, deploy/runner/Terraform runbooks, and
operational gotchas live in `docs/ops/devops.md`, NOT here.

Read `docs/ops/devops.md` first when the task touches: GitHub workflows or
deploy scripts (`.github/**`, `scripts/deploy/**`, `detect_ci_changes.sh`),
Terraform / cloud-init (`infra/**`), the private runner or any VM/network
issue, production schema operations, smoke checks, logs, or incidents.
Skip it for regular feature and test development.

## 7. Local Test Runbook

DB URL source of truth:

- `scripts/dev/test.sh fast` does not need `TEST_DATABASE_URL` and is the default local path when no DB credentials are present.
- `scripts/dev/test.sh doctor` validates the local DB-backed test prerequisites and is the required first check before ad-hoc local DB-backed runs.
- `scripts/dev/test.sh affected --base <sha> --head <sha>` or `scripts/dev/test.sh affected --paths ...` is the default local path for narrow runtime / UX changes because it resolves the minimum validation set from the checked-in validation manifest and auto-starts the default SSH tunnel when the gitignored env file has bastion metadata.
- `scripts/dev/test.sh integration|schema-compat|migration-smoke|all`, `scripts/dev/reset_test_db.sh`, `scripts/dev/reset_remote_test_dbs.sh`, and `scripts/dev/run_db_tests_on_runner.sh` all require a real disposable test DB URL.
- In a plain local shell, `TEST_DATABASE_URL` is normally unset until the operator exports it intentionally. The repo does not contain or infer DB credentials automatically.
- The supported local recovery/bootstrap path is `scripts/dev/write_test_env.sh`, which derives the current app DB credentials from local Terraform outputs and writes a gitignored `.env.test.local`.
- Valid DB-backed patterns are:
  - local tunnel path: `postgresql://<app-user>:<password>@127.0.0.1:15432/qpi_test`,
  - private-runner / DB VM path: `postgresql://<app-user>:<password>@10.131.0.28:5432/qpi_test`.
- In `--mode tunnel`, `scripts/dev/write_test_env.sh` also writes `QPI_DB_VM_HOST` plus `QPI_DB_VM_SSH_PROXY_HOST=<bot-public-ip>` so the DB reset helper can SSH to the private DB VM through the bot VM without requiring a separate DB admin password.
- In GitHub Actions private-runner jobs, `TEST_DATABASE_URL` and `TEST_SCRATCH_DATABASE_URL` come from repo secrets instead of local shell exports.
- Never invent DB credentials. If the concrete value is unavailable in the current environment, stop at `fast` or obtain the real app DB credentials from the operator's secure source before running DB-backed suites.
- There is currently no Yandex Lockbox integration for DB test credentials. The existing recoverable local source is Terraform state / `terraform -chdir=infra output`, and the CI source is GitHub repo secrets.

```bash
uv sync --frozen --extra dev

# Fast local feedback (non-DB suites + deterministic harness):
scripts/dev/test.sh fast

# Preflight local DB-backed prerequisites (env file, tunnel, psql reachability):
scripts/dev/test.sh doctor

# Write a gitignored local DB test env file from Terraform outputs:
scripts/dev/write_test_env.sh --mode tunnel
source .env.test.local

# Create the local SSH tunnel to the remote DB VM explicitly when needed:
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114

# Manual/local shared-db path for ad-hoc work only:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/reset_test_db.sh

# Local ordinary DB integration against the shared test DB:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh integration

# Targeted local validation from changed files:
scripts/dev/test.sh affected --base HEAD~1 --head HEAD

# Targeted local validation from explicit paths:
scripts/dev/test.sh affected --paths services/bot_api/telegram_runtime.py services/bot_api/telegram_notifications.py

# Local schema-compat suite against the shared test DB:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh schema-compat

# Local destructive migration smoke (serialized + reset to qpi_test_scratch):
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh migration-smoke

# Full local ordered suite:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/test.sh all

# Canonical private-runner DB suite:
TEST_DATABASE_URL=postgresql://<user>:<password>@10.131.0.28:5432/qpi_test \
QPI_DB_VM_HOST=10.131.0.28 \
scripts/dev/run_db_tests_on_runner.sh all

# Cleanup stale shared-db sessions when resets fail:
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
scripts/dev/kill-stuck-tests.sh
```

Rules:

- `fast` is the only suite that should normally run on a GitHub-hosted runner.
- Full DB-backed validation should run on the dedicated private self-hosted runner, not over the workstation tunnel.
- For small UI / copy / formatting changes, start with `scripts/dev/test.sh fast` plus the narrow affected pytest files before using `integration` or `all`.
- `doctor` is the mandatory preflight before local DB-backed validation; it checks `.env.test.local`, the `127.0.0.1:15432` tunnel when relevant, and `psql` reachability.
- `doctor`, `affected`, and the local DB-backed suite wrappers auto-load the default `.env.test.local` when `TEST_DATABASE_URL` is still unset; `affected` and the local DB-backed suite wrappers also auto-start the default SSH tunnel when the env file is in tunnel mode and includes `QPI_DB_VM_HOST` plus `QPI_DB_VM_SSH_PROXY_HOST`.
- `affected` uses `scripts/dev/validation_groups.json` as the source of truth for local targeted validation; update that manifest when service ownership or test coverage boundaries change.
- `affected` can escalate from a small runtime-only path to full DB validation when the changed set includes validation-orchestration files (`scripts/dev/test.sh`, workflow selectors, deploy/test wrappers, validation manifest). When checking a product/runtime behavior change inside a larger infra refactor, also run a narrowed affected path for the actual code surface so the result is easier to interpret.
- `scripts/dev/test.sh all` is an expensive reprovision path: it recreates disposable DBs, reapplies schema, and runs unrelated DB manifests. Do not use it as the first local check for a narrowly scoped UX fix.
- If repeated local DB-backed runs are needed in one session, pre-creating the tunnel still reduces local setup churn, but the default wrappers now recover the missing listener automatically when the bastion metadata is available.
- Tunnel auto-start is best-effort only: it works only for tunnel-mode `.env.test.local` files that include `QPI_DB_VM_HOST` plus `QPI_DB_VM_SSH_PROXY_HOST`, and it uses `BatchMode=yes` with the default SSH key path. Missing bastion metadata, a non-default key path, or a key that still needs interactive passphrase entry will still fail fast and require a manual tunnel.
- `integration` and `schema-compat` now reset the disposable DB once per manifest run, not once per file; local and private-runner DB runs rely on per-test truncation for isolation after that reset.
- `affected` still reprovisions the disposable DBs before DB-backed pytest targets; the speedup comes from a smaller selected test set, not from skipping DB recreation.
- `qpi_test_template` is the reusable clean template DB for disposable test runs.
- `qpi_test` and `qpi_test_scratch` are disposable clones of `qpi_test_template`; the reset helpers rebuild the template only when schema / DB-tooling inputs change.
- When `QPI_DB_VM_HOST` is set and `TEST_DATABASE_ADMIN_URL` is unset, `scripts/dev/test.sh integration|schema-compat|migration-smoke|all` automatically uses the DB VM SSH reset path instead of requiring a separate local admin DB password.
- From a workstation, that DB VM SSH reset path requires either direct network reachability to `10.131.0.28` or an SSH proxy host. The supported tunnel-mode bootstrap now writes `QPI_DB_VM_SSH_PROXY_HOST` automatically so the reset helper can hop through the bot VM public IP.
- `scripts/dev/reset_remote_test_dbs.sh` is the canonical full-suite reprovision path from the private runner to the DB VM admin path.
- `tests/db_integration_manifest.txt`, `tests/schema_compat_manifest.txt`, and `tests/migration_smoke_manifest.txt` are the source of truth for DB-backed test grouping.
- `TEST_DATABASE_URL` must include the app DB user because the reset scripts recreate disposable DBs with that user as the database owner; otherwise schema apply fails with `permission denied for schema public`.

Safety guardrails:

- Never apply manual DDL directly in PostgreSQL.
- Production deployments should use the compatibility patch + declarative apply path, not ad-hoc SQL.
- Validate schema changes on clean path (`apply -> drop -> apply`).
- Destructive migration smoke must run only against disposable DB names (`scratch|tmp|disposable`).
- Do not treat a bot deployment as successful unless schema apply and seller/buyer `/start` smoke checks both pass.


## 8. Security and Accepted MVP Risks

Accepted risks:

- Hot wallet single-key custody.
- Broad SSH ingress (`0.0.0.0/0`) at current stage.
- Manual admin handling remains for finance exceptions.

Mandatory controls:

- Immutable ledger trail for all balance-changing operations.
- Admin audit trail (who/what/when).
- Sensitive chat input cleanup where Telegram permissions allow deletion.

## 9. Open Items

- Final production payout broadcasting integration and key lifecycle policy.
- Tighten SSH ingress to operator CIDRs.
- Optional migration from self-signed IP TLS to domain-managed trusted TLS.
- Payload integrity/signature scheme for extension tokens (post-MVP).
- Post-MVP WB correction operation semantics (`Коррекция продаж`, `Коррекция возвратов`).
- Optional extraction of CF services into separate repositories.
- Terraform remote backend strategy for safe CI-driven apply.
- Replace app-level token cipher with managed secret/KMS-backed mechanism.

## 10. Potential Improvements (Deliberately Deferred for MVP)

- Token-at-rest cryptography hardening:
  - replace current app-level reversible token cipher with authenticated encryption + managed KMS/HSM-backed key lifecycle.
  - status: intentionally deferred; current implementation is accepted as an MVP tradeoff.

- Runtime secret strictness hardening:
  - remove insecure default fallbacks for sensitive settings (e.g. cipher/webhook secrets) and fail-fast on unsafe values outside local dev.
  - status: intentionally deferred; current defaults are accepted for MVP-only environments.
