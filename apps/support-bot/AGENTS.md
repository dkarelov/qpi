# Support Bot AGENTS

Last updated: 2026-06-20 UTC

## Scope

- `apps/support-bot/upstream` is a qpi-owned Python fork/import of `DefaultPerson/telegram-support-bot`.
- The support-bot runtime is Telegram-only, private-only, and uses long polling.
- The active support model is **Support Topic**: one Telegram forum topic per Telegram account in the configured support supergroup.
- Persistent support-bot state uses the existing qpi PostgreSQL cluster in schema `support_bot`.
- Redis is ephemeral support-bot FSM/session state and must stay bounded to 512 MB in containerized deployment.
- Telegram Bot API egress uses the first configured URL from `TELEGRAM_API_PROXY_URLS`.
- End-user private-chat UX must be Russian. Staff-facing commands and operational metadata may stay English.

## Local Commands

- Use the nested uv project:
  - `cd apps/support-bot/upstream && uv sync --locked`
  - `cd apps/support-bot/upstream && uv run ruff check .`
  - `cd apps/support-bot/upstream && uv run mypy app/config.py app/bot/storage.py app/bot/support_topics.py app/bot/telegram_client.py`
  - `cd apps/support-bot/upstream && uv run pytest`
- Docker image build:
  - `docker build -f apps/support-bot/Dockerfile -t qpi-support-bot:local apps/support-bot`
- The old Node/Mongo commands are obsolete for the active runtime.

## Runtime Defaults

- The bot keeps pending Telegram updates on startup by deleting the webhook with `drop_pending_updates=False`.
- `/start` currently does not create a topic on its own; Support Topic creation happens on the first real support message.
- `/start` payloads may carry qpi role/topic/reference context; metadata is reflected in topic titles and pinned topic metadata.
- Text, media, and album forwarding share the Support Topic service seam.
- Closed Support Topics reopen when the user writes again.
- Banned Telegram accounts are ignored until unbanned by staff action.
- Old Mongo data, `/open`, orphan-ticket recovery, old ticket ids, private staff group support, and old queue preservation are out of scope for this runtime.

## Environment

- `SUPPORT_BOT_TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `SUPPORT_BOT_GROUP_ID`: topic-enabled support supergroup id.
- `SUPPORT_BOT_OWNER_ID`: owner Telegram id.
- `SUPPORT_BOT_DEV_IDS`: optional developer ids; omitted value falls back to owner behavior in the app.
- `SUPPORT_BOT_DATABASE_URL` or `DATABASE_URL`: existing qpi PostgreSQL cluster connection string.
- `SUPPORT_BOT_DB_SCHEMA`: default `support_bot`.
- `SUPPORT_BOT_REDIS_DB`: default `7`.
- `TELEGRAM_API_PROXY_URLS`: HTTP(S) proxy list used for Telegram Bot API egress.

## Optional Capabilities

- Newsletter registration is available as an explicit support-bot service surface and keys subscribers by Telegram account id.
- Policy rules remain disabled by default with `POLICY_ENABLED=false`; enabling them must not change qpi's baseline support flow unless a policy file is configured.
- LLM draft support remains disabled by default with `AI_PROVIDER=none`; the production dependency set includes the OpenAI-compatible client so drafts can be enabled later through config.

## Deploy

- Terraform for support-bot infra lives under `infra/support_bot*.tf`.
- The runtime deploy entrypoint is `scripts/deploy/support_bot.sh`.
- The runtime remains private-only and long-polls Telegram, so there is no webhook or public listener.
- Production deploys are expected to run from the existing private runner workflow, not from the workstation.
- Manual workstation deploys to the private-only VM must set `SUPPORT_BOT_VM_SSH_PROXY_HOST=<qpi-bot-public-ip>` so SSH/scp can hop through the always-on qpi bot VM.
- `/opt/support-bot/current` is a symlink managed by the deploy wrapper; it must never be pre-created as a real directory.
- Deploy smoke checks verify Redis PING, PostgreSQL schema access, and Telegram `getMe` through `TELEGRAM_API_PROXY_URLS`.

## Upstream Update Policy

- Upstream updates are manual, not automatic.
- Imported upstream source: `https://github.com/DefaultPerson/telegram-support-bot`.
- Current imported upstream commit: `b74e7b73107ea1f59cc05b878a488470fc84bd6b`.
- Apply future upstream changes selectively inside the qpi fork and keep qpi behavior/tests authoritative.
