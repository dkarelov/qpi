# QPI AGENTS

Last updated: 2026-04-02 UTC

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

`AGENTS.md` is the single project source of truth for:

- current product requirements,
- implemented decisions,
- operating rules,
- deployed infrastructure state,
- runbooks and safeguards.

Current repo scope:

- qpi marketplace runtime (Python + PostgreSQL),
- companion support-bot runtime (Node/TypeScript + MongoDB) under `apps/support-bot`,
- shared Terraform, runner, and deploy conventions.

Documentation rules:

- Keep `AGENTS.md` aligned with actual code and Terraform state.
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
- isolated runtime, VM, and MongoDB state,
- Telegram-only in V1,
- Russian UX text,
- no Signal, web chat, LLM, or backup automation in V1,
- normal private Telegram group is accepted for `staffchat_id`; supergroup is optional,
- default qpi support-bot template ships with `clean_replies=true` and `auto_close_tickets=true`,
- marketplace buyer/seller screens may deep-link into the support bot when `SUPPORT_BOT_USERNAME` is configured,
- support-bot tickets are one shared queue and carry actor/entity context from the marketplace bot instead of role-based routing.

## 3. Implemented System Components

Runtime services:

- `services/bot_api`: always-on webhook bot runtime on VM.
- `services/daily_report_scrapper`: Cloud Function, hourly WB report sync.
- `services/order_tracker`: Cloud Function, 5-minute assignment lifecycle orchestrator.
- `services/blockchain_checker`: Cloud Function, 5-minute seller collateral top-up matcher.
- `services/worker`: placeholder runtime (legacy/no critical ownership).
- `apps/support-bot/*`: companion private-only long-polling support desk stack with vendored upstream app, local Docker/compose overlay, and dedicated deploy workflow.

Shared layers:

- `libs/domain/*`: transactional domain services (plain SQL).
- `libs/domain/seller_workflow.py`: backend seller activation/unpause facade that performs live WB product checks before mutating listing state.
- `libs/integrations/*`: WB/TonAPI/FX clients.
- `libs/config/settings.py`: runtime settings contracts.
- `libs/logging/setup.py`: YC-compatible structured logging.
- `services/bot_api/telegram_notifications.py`: bot-runtime-only Telegram notification renderer; shared outbox enqueue/claim logic stays in `libs/domain/notifications.py`.
- `libs/db/*`: pool and schema tooling.
- `scripts/dev/*`: canonical local reset/test/export wrappers.
- `scripts/deploy/runtime.sh`: canonical bot VM rollout entrypoint.
- `scripts/deploy/function.sh`: canonical code-only Cloud Function rollout entrypoint.
- `scripts/deploy/preflight.sh`: canonical shared deploy preflight entrypoint for runtime, function, and support-bot rollouts.
- `scripts/deploy/schema_remote.sh`: canonical production-schema cleanup/assert/apply entrypoint over the bot-VM bastion.
- `scripts/deploy/support_bot.sh`: canonical support-bot image rollout entrypoint.
- `scripts/deploy/private_runner.sh`: canonical on-demand private runner lifecycle entrypoint for CI/deploy jobs.

Persistence and schema:

- PostgreSQL + `psqldef`.
- `schema/schema.sql` is the only schema source of truth.
- Support bot MongoDB state lives on the support-bot VM boot disk under `/var/lib/support-bot/mongodb`.

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
- Listing creation input:
  - `wb_product_id`,
  - cashback in RUB,
  - slot count,
  - search phrase,
  - optional review phrase pool (`0..10` phrases).
- Seller listing-create input is a single comma-separated message:
  - `wb_product_id, cashback_rub, slot_count, search_phrase, review_phrase_1, ... , review_phrase_10`.
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

- Buyer enters shop by deeplink `shop_<slug>` or by saved shops menu.
- Buyer can reserve slot only on active listings.
- Buyer-facing primary CTA for an active listing is `Купить`.
- Buyer-facing listing screens show buyer-visible title, WB subject, description, photo, sizes, characteristics, cashback in RUB with approximate percent, and `Цена` in RUB.
- Buyer-facing listing screens/cards must not expose WB article (`Артикул WB` / `Артикул ВБ`), WB brand, or WB source title.
- Buyer receives setup token (base64 JSON array):
  - `[search_phrase, wb_product_id, 1, wb_brand_name]`, where `wb_brand_name` is an empty string when unavailable.
- Buyer submits verification token (base64 JSON array):
  - `[order_id, ordered_at]`, where `ordered_at` is an ISO datetime; timezone-bearing values are accepted and normalized to UTC.
- After pickup, buyer receives review setup token (base64 JSON array):
  - `[wb_product_id, review_phrase_1?, review_phrase_2?]`, where phrases are omitted when the seller did not provide them.
- Buyer submits review confirmation token (base64 JSON array):
  - `[wb_product_id, reviewed_at, 5, review_text]`, where `reviewed_at` is an ISO datetime; timezone-bearing values are accepted and normalized to UTC.
- Verification token must be submitted within 4 hours of reservation.
- `order_id` is globally unique (`1 order_id = 1 slot`).
- Review confirmation is mandatory after pickup. Without it, cashback stays frozen even after the unlock timer has passed.
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
- Unlock timer credits buyer balance and transitions assignment to `withdraw_sent` (`Выплачен`).

### 4.4 Admin and finance rules

- Admin operations are Telegram-driven and auditable.
- Withdrawals use one shared requester model for buyers and sellers and require admin decision path:
  - open request,
  - reject with reason, or
  - enter tx hash for a completed transfer.
- Withdrawal completion is single-step:
  - admin enters tx hash only after sending funds,
  - bot verifies the tx hash on-chain against the configured TON USDT payout wallet, requester address, and exact amount,
  - only a verified tx completes the request,
  - failed/missing tx verification leaves the request pending for retry.
- Every new buyer or seller withdrawal request sends an admin push notification with requester role, Telegram identity, amount, and request number.
- Manual deposit is supported for exception handling/bonuses/corrections.
- Manual deposit input supports role aliases:
  - `seller` maps to `seller_available`,
  - `buyer` maps to `buyer_available`.
- External reference for manual deposit is mandatory audit metadata and can be either:
  - free-form reason/comment,
  - tx reference (e.g. `tx:...`).
- `system_payout` balance provisioning remains an accepted implementation shortcut for externally funded credits, but every such top-up must create an immutable audit record in `system_balance_provisions`.

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
  - incoming amount must be `>= expected_amount`, on-time.
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

### 4.7 Telegram UX rules (must be preserved)

- Every button press must produce visible feedback:
  - message edit, new reply, or alert.
- Silent no-op callback behavior is forbidden.
- Menus are tree-structured (no flat action panel).
- Callback-driven navigation is immutable/linear:
  - button presses retire the old inline keyboard,
  - the bot sends a new screen message instead of editing the previous one.
- Standard screen layout:
  - title,
  - italic call to action immediately below the title,
  - main content blocks separated by empty lines,
  - optional italic note at the bottom only when it adds non-obvious next steps or issue guidance.
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
  - top section shows only WB article, cashback, search phrase, plan/in-progress, shop link, collateral, and activity status,
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
  - buyer balance screen shows only `Доступно для вывода` and `В процессе вывода`,
  - if the buyer already has an active withdrawal request, new withdrawal actions are hidden and the screen shows that request plus a cancel action,
  - buyer withdrawal history is full paginated history with `<` / `>` navigation, timestamps, comments, and tx hash when available,
  - irrelevant actions must be hidden when they cannot be used in the current state (for example withdrawal buttons when withdrawable balance is zero),
  - purchase flow contains explicit order-token submit, review-token submit, and cancel-purchase actions when relevant.
- Seller notifications:
  - pickup notification can indicate that buyer review is still pending,
  - after review confirmation the seller receives a dedicated notification containing the confirmed rating, review text, and confirmation time.
- All user-facing timestamps are rendered in `MSK` (`Europe/Moscow`).
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

- Marketplace services remain Python-only. The companion support-bot runtime is Node/TypeScript and isolated under `apps/support-bot`.
- Marketplace dependency and environment management are `uv`-based; `.venv` remains the runtime path, but `uv.lock` is the source of truth.
- Support-bot dependency management is `npm` + upstream `package-lock.json`, with Node 24 as the qpi target version.
- `requirements.txt` is generated from `uv.lock` for Cloud Function/Terraform compatibility and is never hand-edited.
- DB access: `psycopg3` + plain SQL only (no ORM).
- Schema changes only through `schema/schema.sql` + `psqldef`.
- Infrastructure mutations are Terraform-only from `infra/`.
- Code-only deploy entrypoints are `scripts/deploy/runtime.sh`, `scripts/deploy/function.sh`, and `scripts/deploy/support_bot.sh`; broader infra mutations still remain Terraform-only.
- Cloud Function packaging must be service-scoped to avoid unrelated redeploys.
- DB-backed CI/deploy execution is designed around a dedicated private self-hosted GitHub runner VM; GitHub-hosted runners only handle fast suites and bootstrap/start-stop orchestration.
- Release-grade marketplace runtime and function deploys now reuse the private runner after DB validation so the network-heavy rollout happens from the same YC region as the targets.
- Marketplace deploy workflows now run an explicit predeploy gate before rollout and publish immutable runtime/function artifacts before the deploy step consumes them.
- Marketplace bot runtime is webhook-based. Companion support-bot runtime uses long polling and remains private-only.
- Support-bot images are now published to a dedicated Yandex Container Registry repository and pulled on the VM during rollout instead of being copied as `docker save` archives.
- `SUPPORT_BOT_USERNAME` is an optional marketplace bot runtime env var; when set, seller/buyer screens can build deep links into the support-bot using the public ref contract above.
- Expected load target: ~100 concurrent users.

## 6. Infrastructure State (Current)

YC folder:

- `b1gmeblqlrrvm912n1uq`

Compute and networking:

- Bot runtime: instance group (`qpi-bot-ig`), size 1, preemptible.
- Bot public IP: `158.160.187.114`.
- Support bot runtime: Terraform-defined private-only instance group (`qpi-support-bot-ig`), size 1, preemptible; resolve live IDs with `terraform -chdir=infra output` after apply.
- Support-bot image registry: Terraform-managed Yandex Container Registry (`qpi-support-bot-registry`) with immutable SHA-tagged images under repository `support-bot`.
- DB VM: private-only (`10.131.0.28`), non-preemptible.
- Private runner VM: `qpi-private-runner` (`fv47djh2aqv62pq449mq`), preemptible, on-demand, private IP `10.130.0.23`.
- Private runner public IP is ephemeral NAT and must be resolved dynamically through `yc`, not hardcoded.
- Private subnet: `10.131.0.0/24` with NAT gateway egress.

Serverless functions:

- `qpi-daily-report-scrapper` (`d4ee0tvqv3jutd2kk3ng`) + hourly trigger.
- `qpi-order-tracker` (`d4edjmt28evde0urt9q4`) + 5-minute trigger.
- `qpi-blockchain-checker` is Terraform-managed in `infra/serverless.tf`.
- CF memory profile: `128 MB`.

Resource snapshot IDs (reference):

- Bot IG: `cl17ilrmf3ukgtg14gbe`
- DB instance: `fv4drfqh36622f5lf1vc`
- NAT gateway: `enpkq1bnf0ij8jcjmf7s`
- Private route table: `enpmdt4gs3gav0qd4nce`
- Private subnet: `fl8oled9cdd9u2efqaae`
- Logging group: `e2345psnoc0appog5lil`

Operational note:

- Always check latest values via `terraform -chdir=infra output` before sensitive operations.

## 7. Runbooks

### 7.1 Terraform

```bash
uv sync --frozen --extra dev

terraform -chdir=infra init
terraform -chdir=infra fmt
terraform -chdir=infra validate

GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" \
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra plan

GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" \
TF_VAR_cf_token_cipher_key="<cipher-key>" YC_TOKEN="$(yc config get token)" \
terraform -chdir=infra apply

terraform -chdir=infra output
```

Code-only deploy rule:

- If only marketplace Python/runtime code changed, use `scripts/deploy/runtime.sh` or `scripts/deploy/function.sh <service>` instead of `terraform apply`.
- If only support-bot app/runtime code changed, use the support-bot deploy workflow or `scripts/deploy/support_bot.sh` instead of `terraform apply`.
- The runner VM intentionally uses ephemeral NAT instead of a reserved static external IP because the folder hit the external static IP quota during rollout.
- `ubuntu_2404_lts_image_id` is pinned in Terraform to avoid unrelated bot/DB VM replacements when the Ubuntu family image advances.

### 7.2 SSH and DB access

```bash
yc compute instance-group list-instances --name qpi-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -i ~/.ssh/id_rsa ubuntu@158.160.187.114
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@10.131.0.28
```

Support-bot private-only access:

```bash
yc compute instance-group list-instances --name qpi-support-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
```

Manual support-bot deploy via workstation bastion:

```bash
SUPPORT_BOT_VM_SSH_PROXY_HOST=158.160.187.114 \
scripts/deploy/support_bot.sh <image-archive> <image-tag>
```

Support-bot live verification:

```bash
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@158.160.187.114" -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
readlink -f /opt/support-bot/current
systemctl is-active support-bot.service
sudo docker inspect -f '{{.Config.Image}}' current-supportbot-1
sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml \
  exec -T mongodb mongosh --quiet --eval 'db.adminCommand({ ping: 1 }).ok' mongodb://127.0.0.1:27017/admin
```

DB tunnel (default session policy):

```bash
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/.ssh/id_rsa -L 127.0.0.1:15432:10.131.0.28:5432 ubuntu@158.160.187.114
ss -ltnp | rg ':15432\\b'
```

Rules:

- Keep tunnel active during active development sessions.
- Recreate tunnel if listener is missing before DB operations.
- Before any local DB-backed test run, verify the listener first with `ss -ltnp | rg ':15432\\b'`; a missing tunnel can look like a hung pytest/psqldef run instead of failing fast.
- Operator workstation has `psql` available (`PostgreSQL 16.13`); prefer direct `psql` checks over ad-hoc Python probes for DB inspection, schema verification, and lock/activity checks.
- If a missing local tool would materially improve speed, reliability, or operator clarity, ask the operator to install it instead of defaulting to a slower workaround.
- DB VM security group allows SSH from the private runner security group specifically so `reset_remote_test_dbs.sh` can recreate disposable test DBs through the DB-admin path.
- Support-bot security group allows SSH from the private runner SG and the qpi bot SG; there is no direct public SSH path for the support-bot VM.
- Support-bot security group also keeps TCP/22 open to `0.0.0.0/0` for Yandex instance-group SSH health checks; that does not create direct public access because the VM has no public IP.
- `scripts/deploy/runtime.sh` expects `BOT_WEBHOOK_SECRET_TOKEN` in the caller environment even though the live bot env file stores the value under `WEBHOOK_SECRET_TOKEN`; map the name explicitly when reusing values from `/etc/qpi/bot.env`.
- The support-bot deploy workflow currently reuses `BOT_VM_SSH_PRIVATE_KEY`; keep that secret valid for both bot and support-bot VM access unless a separate support-bot key is intentionally introduced and verified.

### 7.3 Schema operations

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m libs.db.runtime_schema_compat apply
uv run python -m libs.db.schema_cli plan
uv run python -m libs.db.schema_cli apply
uv run python -m libs.db.schema_cli cleanup-plan
uv run python -m libs.db.schema_cli cleanup-apply
uv run python -m libs.db.schema_cli assert-clean
uv run python -m libs.db.schema_cli drop
uv run python -m libs.db.schema_cli export
```

Rule:

- Any bot release that starts reading new DB columns must apply schema before the bot process is restarted.
- For production-like legacy drift, run `python -m libs.db.runtime_schema_compat apply` before declarative `schema_cli apply`, and use `schema_cli cleanup-apply` to drop obsolete objects after the additive migration step has backfilled live data.
- Long-lived environments are expected to match `schema/schema.sql` exactly after cleanup; obsolete columns such as `withdrawal_requests.buyer_user_id` and `wb_report_rows.srid` are migration-only artifacts and must not remain in runtime-supported schemas.
- Operator-driven production schema apply remains the SSH-tunnel path to `127.0.0.1:15432`.
- `scripts/deploy/schema_remote.sh` is the canonical production path for `cleanup-plan`, `cleanup-apply`, `apply`, and `assert-clean` against the live DB through the bot-VM SSH bastion.
- CI/runtime/function deploys must assert that production schema cleanup drift is empty before code rollout; if drift remains, deployment stops until cleanup is applied.
- CI skips production schema apply entirely when no schema-related files changed (`schema/**`, `libs/db/**`, deployment schema runner).

### 7.4 Runtime smoke checks

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
uv run python -m services.bot_api.main --seller-command "/start" --telegram-id 10001 --telegram-username seller
uv run python -m services.bot_api.main --buyer-command "/start" --telegram-id 10002 --telegram-username buyer
uv run python -m services.daily_report_scrapper.main --once
uv run python -m services.order_tracker.main --once
uv run python -m services.blockchain_checker.main --once
```

Support-bot local validation:

```bash
cd apps/support-bot/upstream
npm ci
npm run build
npm test
docker compose -f ../compose.dev.yml up -d
```

Support-bot live behavior defaults:

- `clean_replies=true`: user-facing staff replies are plain message bodies without greeting/signature wrappers.
- `auto_close_tickets=true`: a successful staff reply closes the ticket, so `/open` will no longer list it.
- Staff-facing ticket headers omit Telegram `language_code`; it is Telegram client metadata, not actual message-language detection.
- If `support-bot.service` fails during startup with `supportbot is missing dependency mongodb`, recover by starting `mongodb` first, waiting for a healthy container, then starting `supportbot`, and only then reconciling the systemd unit.
- Avoid overlapping ad-hoc support-bot image builds on the VM. Concurrent remote `docker build` attempts can contend on containerd refs and stall or wedge the rollout until stale build processes are killed.

### 7.5 Test runbook

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

### 7.6 Logs and incidents

Core correlation fields:

- `telegram_update_id`, `shop_id`, `listing_id`, `assignment_id`, `withdrawal_request_id`, `ledger_entry_id`.

Common checks:

- webhook handler failures in bot runtime,
- WB API errors in scrapper/tracker,
- withdrawal backlog and stuck statuses,
- payout send/reject events by request id and tx hash.

Runbook shortcuts:

- Bot outage:
  - `sudo systemctl status qpi-bot.service`
  - `curl -fsS http://127.0.0.1:18080/healthz`
  - verify Telegram `getWebhookInfo` URL/secret alignment.
- CF degradation:
  - inspect logs for `daily_report_scrapper`, `order_tracker`, `blockchain_checker`,
  - verify DB connectivity and runtime env key alignment.
- Payout incident:
  - inspect request detail + ledger/audit rows,
  - notify buyer and annotate admin reason,
  - stop further payout actions if ledger consistency is in doubt.

## 8. CI/CD Overview

Workflows:

- `.github/workflows/ci.yml`:
  - PR-focused validation workflow plus manual dispatch,
  - runs fast tests, `actionlint`, and `shellcheck` on GitHub-hosted runners,
  - starts the private runner only for trusted same-repo PRs / manual runs that actually need DB-backed validation, and now overlaps runner boot with fast validation,
  - skips migration smoke unless schema-related files changed,
  - intentionally ignores support-bot-only paths so companion Node changes do not trigger qpi DB validation.
- `.github/workflows/post_merge.yml`:
  - single post-merge orchestrator for `main` pushes and manual reruns,
  - runs fast validation once,
  - starts the private runner only when DB-backed validation is required, and now overlaps that boot with fast validation,
  - runs targeted or full DB-backed validation once depending on changed files,
  - runs a dedicated marketplace predeploy gate on the private runner before any rollout,
  - builds immutable runtime/function artifacts once on GitHub-hosted runners,
  - runs runtime and Cloud Function deploy jobs from the private runner after validation and predeploy succeed,
  - `workflow_dispatch` supports `full_validation=true` to force the old all-up DB validation path.
- `.github/workflows/deploy_runtime.yml`:
  - manual runtime deploy path with two modes,
  - `auto` resolves to `hotfix` only for SHAs already on `main` with a successful push-event `post_merge` run for that exact SHA,
  - both modes now run an explicit preflight gate and build a single runtime artifact before any rollout step,
  - `hotfix` keeps GitHub-hosted deploy execution after the artifact is built,
  - `release-grade` keeps fast validation plus full DB-backed validation before rollout and now executes rollout from the private runner,
  - `preflight_only=true` runs checks plus artifact build without touching production,
  - the target SHA is checked out directly in the workflow, so operator reruns are no longer limited to `HEAD`.
- `.github/workflows/deploy_functions.yml`:
  - manual function-only deploy path,
  - keeps release-grade DB-backed validation on the private runner,
  - runs a dedicated preflight gate on the private runner before publish,
  - builds immutable function bundles once on GitHub-hosted runners and publishes those exact bundles from the private runner,
  - `preflight_only=true` runs checks plus bundle build without publishing.
- `.github/workflows/support_bot_ci.yml`:
  - support-bot PR/manual workflow,
  - runs Node 24 build/test plus production image build,
  - also runs repo workflow/shell lint so support-bot workflow/script changes are validated without triggering qpi DB suites.
- `.github/workflows/support_bot_deploy.yml`:
  - support-bot `main` auto-deploy plus manual dispatch,
  - builds and pushes the image to Yandex Container Registry on GitHub-hosted runners,
  - runs a dedicated preflight gate on the private runner before rollout,
  - reuses the existing private runner only for private-network deployment into the support-bot instance group,
  - `preflight_only=true` builds/pushes the image and runs checks without touching the VM release symlink.
- `.github/workflows/private_runner_keepalive.yml`:
  - weekly start of the dedicated private runner,
  - validates runner registration / dispatch path.
- `.github/workflows/deploy_terraform.yml`:
  - terraform validate/plan on push,
  - apply only via explicit manual dispatch guard.
- `.github/dependabot.yml`:
  - weekly `github-actions` checks for workflow action references,
  - opens reviewable PRs for action upgrades before platform deprecations become workflow noise.

Private dependency handling:

- `GH_TOKEN` is the canonical auth variable for private GitHub dependencies.
- Existing `TOKEN_YC_JSON_LOGGER` is still accepted and mapped to `GH_TOKEN` by repo wrappers for backward compatibility.
- `scripts/common/setup_private_git_auth.sh` configures git URL rewriting before `uv` operations that need the private dependency.
- `requirements.txt` is generated from `uv.lock` and kept only as a compatibility artifact for Cloud Function/Terraform packaging.

Private runner / workflow gotchas:

- Repo secrets `PRIVATE_RUNNER_SSH_PRIVATE_KEY`, `DB_VM_SSH_PRIVATE_KEY`, and `BOT_VM_SSH_PRIVATE_KEY` should be stored as base64-encoded private key material. The scripts accept raw / escaped / base64 formats, but base64 is the canonical GitHub Actions format because multiline PEM secrets were brittle during rollout.
- Deploy/bootstrap scripts configure `yc` from `YC_TOKEN` + `YC_FOLDER_ID` on every run; do not assume `yc init` or a preexisting profile on GitHub-hosted or self-hosted runners.
- Shared deploy/bootstrap setup now lives in `.github/actions/setup-qpi-deploy`; use it for runtime/function deploy jobs instead of reintroducing per-workflow tool-install snippets.
- `scripts/deploy/preflight.sh` is the shared predeploy gate. Runtime/function/support-bot workflows should fail there before starting rollout rather than discovering SSH/schema/host problems mid-deploy.
- GitHub-hosted validation jobs cache `~/.cache/uv` keyed by Python version and `uv.lock` to reduce repeated dependency download cost.
- Fast validation is centralized in reusable workflow `.github/workflows/_fast_validation.yml`; keep PR, post-merge, and manual deploy validation behavior aligned there instead of editing each caller separately.
- When sourcing a shared shell helper from `scripts/**`, keep the `# shellcheck source=...` hint repo-relative (for example `scripts/dev/test_db_template_lib.sh`), not workstation-absolute; CI shellcheck runs against the checked-out repo tree and will fail on local absolute paths even when the script itself works.
- `.github/actionlint.yaml` must keep the custom `qpi-private` self-hosted runner label declared or `actionlint` will fail the validation path even when the workflows are otherwise correct.
- Runner-touching concurrency is scoped to runner jobs, not whole workflows. Whole-workflow concurrency caused unrelated workflows to cancel each other during rollout.
- `.github/workflows/post_merge.yml` now uses workflow-level concurrency on `main` with stale-run cancellation; `private_runner.sh ensure-ready` sets a max-session shutdown failsafe so canceled runs do not strand the runner VM indefinitely.
- When debugging CI/deploy behavior, prefer `workflow_dispatch` runs one at a time on `main` instead of relying on overlapping push-triggered workflows.
- The private runner self-updates its GitHub runner binary automatically; the first bring-up after a version change can briefly restart the runner before it comes back online.
- Runner cloud-init now preinstalls `yc`, `uv`, and `psqldef`; workflows still keep defensive fallback installs until the runner VM is reprovisioned with the updated image bootstrap.
- `private_runner.sh ensure-ready` now installs and refreshes the runner-local `qpi-private-runner-autoshutdown.timer` controller on the VM, then heartbeats it after the runner reports online.
- The private runner now powers itself off locally after 60 minutes without active `Runner.Worker` processes or interactive SSH sessions; workflows no longer SSH back in just to schedule idle shutdown.
- `private_runner.sh` still sets a 120-minute `shutdown -h +...` max-session failsafe on each `ensure-ready` call so canceled or wedged workflows cannot strand the VM indefinitely.
- The post-merge orchestrator still skips docs-only (`AGENTS.md`, `docs/**`) and pure test-only changes on `main`, but validation-orchestration changes (`detect_ci_changes`, targeted-validation manifest/scripts, workflow selectors) now trigger post-merge validation without forcing runtime/function deploys.
- `detect_ci_changes` and `scripts/dev/test.sh affected` share the same checked-in validation manifest; keep local targeted validation and CI/post-merge selection aligned there instead of duplicating trigger logic.
- Runtime-only Telegram copy/render work belongs in `services/bot_api/telegram_notifications.py`; changing that file should stay in the runtime validation/deploy surface. Shared enqueue/outbox changes in `libs/domain/notifications.py` still affect `order_tracker` and therefore still pull the shared DB validation / function-target selection path.
- Validation-orchestration changes can still boot the private runner and run DB-backed validation on `main`; that is intentional because selector changes must be verified end to end against the private-runner path.
- `gh run watch <run-id> --exit-status` is the preferred operator check after a push, but `start-private-runner` can sit in progress for a while during VM boot and runner registration; do not treat that alone as a failure unless the job times out or subsequent status turns red.
- A code push to `main` can still take several extra minutes after local work is finished because `post_merge` still waits for private DB validation and the selective deploy jobs, but private-runner boot now overlaps with fast validation instead of serializing behind it.
- A push that changes workflow or deploy-orchestration files can trigger extra workflows beyond `post_merge`. In particular, `.github/workflows/deploy_terraform.yml` is itself a watched path for the `Deploy Terraform` push workflow, so workflow edits may require checking two green runs on `main`, not one.
- `gh run view <run-id> --job <job-id> --log` does not stream in-progress job output; for live inspection use `gh run watch` or `gh run view <run-id> --json jobs,status,conclusion,url` and look at step states instead.
- `gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs` currently returns plain text from the blob backend, not a zip archive; if `gh run view --job --log` is sparse, fetch that endpoint directly and grep the text instead of trying to unzip it.
- `gh variable` has no `get` subcommand. Use `gh variable list`, `gh variable set`, or `gh api` when verifying repo-level workflow vars such as `SUPPORT_BOT_USERNAME`.
- In `post_merge`, a job line like `deploy-functions in 0s` means the job was intentionally skipped because no function targets changed; it is not an error condition.
- In the current optimized path, a successful `post_merge` run will often spend most of its time in the predeploy/runtime/function rollout phases; fast validation and private-runner boot overlap, so the deploy-specific timing summary is the source of truth when checking for regressions.
- Release-grade marketplace deploy scripts now print phase timing key-values and write a Markdown timing table to `${GITHUB_STEP_SUMMARY}` when available; use those timings before guessing whether runner boot, packaging, upload, schema, or rollout got slower.
- In manual `post_merge` reruns, `full_validation=true` forces the full DB validation path but does not invent deploy targets; runtime/function deploy jobs still follow the resolved change/deploy target set and may remain skipped.
- Runtime deploys merge explicit env overrides into `/etc/qpi/bot.env`; if `SUPPORT_BOT_USERNAME` is not passed through the workflow env, a deploy will silently blank the support deep-link config.
- Bot runtime rollout now reuses a lockfile-keyed shared `.venv` under `/opt/qpi/shared-venvs` when `pyproject.toml` / `uv.lock` are unchanged; code-only deploys still unpack a fresh release and run the same health/smoke checks, but they no longer rebuild dependencies every time.
- The shared-venv deploy optimization helps only after the target lock hash already exists on the VM. The first deploy for a new `uv.lock` / `pyproject.toml` fingerprint still has to build that environment once, so do not expect the first post-change rollout to show the full timing win.
- After fixing workflow/env propagation for an optional runtime feature, verify the live target directly (`/etc/qpi/bot.env`, service health, and one relevant UX path) instead of trusting the workflow green status alone.
- Workflow action references target Node24-ready `actions/checkout@v6` and `actions/setup-python@v6`; keep the private runner on `v2.329.0` or newer for `checkout@v6` compatibility.
- Function bundle publishing requires `zip`; it is installed both in runner cloud-init and defensively in the GitHub-hosted deploy-functions workflow.
- Runtime and function deploy wrappers prune old `.artifacts` outputs with retention knobs so the private runner workspace does not grow without bound.
- GitHub Actions `Node 20` deprecation warnings refer to GitHub-provided JavaScript actions such as `actions/checkout` / `actions/setup-python`, not to the QPI application stack.

Active development rule:

- During the active development phase, completed runtime/code changes must be verified with the relevant repo test/build/lint steps first, then committed and pushed by default unless the operator explicitly says not to push.
- If the operator does not explicitly opt out, treat `commit + push + verification summary` as part of finishing the task, not as optional follow-up.
- Deploy completed changes by default unless the operator explicitly says not to deploy.
- When the expected code diff is small but the default finish path is expensive, call that out before starting the push/deploy stage so the operator can choose between `local verification only` and `full rollout`.
- When a deploy is expected, do not stop at a successful push or workflow trigger: verify the live target state after rollout (service health, active release/image, and at least one relevant smoke check) before considering the task complete.
- If a deployment fails, treat fixing the deployment path as part of completing the task instead of stopping after the failed rollout.

## 9. Security and Accepted MVP Risks

Accepted risks:

- Hot wallet single-key custody.
- Broad SSH ingress (`0.0.0.0/0`) at current stage.
- Manual admin handling remains for finance exceptions.

Mandatory controls:

- Immutable ledger trail for all balance-changing operations.
- Admin audit trail (who/what/when).
- Sensitive chat input cleanup where Telegram permissions allow deletion.

## 10. Open Items

- Final production payout broadcasting integration and key lifecycle policy.
- Tighten SSH ingress to operator CIDRs.
- Optional migration from self-signed IP TLS to domain-managed trusted TLS.
- Payload integrity/signature scheme for extension tokens (post-MVP).
- Post-MVP WB correction operation semantics (`Коррекция продаж`, `Коррекция возвратов`).
- Optional extraction of CF services into separate repositories.
- Terraform remote backend strategy for safe CI-driven apply.
- Replace app-level token cipher with managed secret/KMS-backed mechanism.

## 11. Potential Improvements (Deliberately Deferred for MVP)

- Token-at-rest cryptography hardening:
  - replace current app-level reversible token cipher with authenticated encryption + managed KMS/HSM-backed key lifecycle.
  - status: intentionally deferred; current implementation is accepted as an MVP tradeoff.

- Runtime secret strictness hardening:
  - remove insecure default fallbacks for sensitive settings (e.g. cipher/webhook secrets) and fail-fast on unsafe values outside local dev.
  - status: intentionally deferred; current defaults are accepted for MVP-only environments.
