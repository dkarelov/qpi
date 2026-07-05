# QPI Product Requirements

Product requirements reference for the qpi marketplace and companion support-bot.
Read the section matching the feature area you are changing; `AGENTS.md` holds the
per-session operating rules and points here.

Record only current behavior and active decisions. Do not keep phase-by-phase
history or superseded evolution notes here.

## Glossary for Telegram UX

- `Объявление` = seller-created buyer-facing offer for one WB product.
- `Покупка` = buyer reservation/work item tied to one announcement.
- `Активно` = user-facing wording for active/open availability or status.
- `Support Topic` = canonical support conversation unit: one Telegram forum topic per Telegram Account in the support supergroup.

Domain vocabulary (English terms, avoided synonyms, relationships) lives in root `CONTEXT.md`.

## 1. Product Scope (Current MVP)

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

## 2. Functional Requirements and Rules

### 2.1 Seller rules

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

### 2.2 Buyer rules

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

### 2.3 Assignment lifecycle rules

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

### 2.4 Admin and finance rules

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
- Admin `⚠️ Исключения` covers both deposit anomalies and blocked buyer review confirmations; admins can manually verify a matching review-confirmation token for `P<assignment_id>` and audibly move the assignment to `picked_up_wait_unlock`.

### 2.5 Seller top-up auto-confirmation rules (blockchain checker)

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

### 2.6 WB token and report rules

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

### 2.7 Telegram UX rules (must be preserved)

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

### 2.8 Money, precision, and FX rules

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
