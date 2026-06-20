# qpi Support Bot

This directory contains the qpi-owned Python support-bot runtime imported from `DefaultPerson/telegram-support-bot`.

## Pinned upstream

- Repository: `https://github.com/DefaultPerson/telegram-support-bot`
- Imported source lives in `apps/support-bot/upstream`
- Current imported upstream commit: `b74e7b73107ea1f59cc05b878a488470fc84bd6b`
- Upstream updates are manual. Reconcile future upstream commits selectively into this fork and keep qpi tests authoritative.

## Runtime model

- Telegram-only, private-only, long polling.
- Canonical support unit: **Support Topic**, one Telegram forum topic per Telegram Account in the configured support supergroup.
- User messages are delivered from private chat into the matching Support Topic.
- Staff replies are handled in the support supergroup topic.
- Persistent state uses the existing qpi PostgreSQL cluster and app-owned schema `support_bot`.
- Redis is ephemeral FSM/session state. The container deployment caps it with `--maxmemory 512mb`.
- Telegram Bot API egress uses `TELEGRAM_API_PROXY_URLS`; deploy and preflight validate `getMe` through the configured proxy.
- End-user private-chat text is Russian. Staff commands and operational metadata may remain English.

Out of scope for the new runtime:

- old Mongo data,
- `/open`,
- orphan-ticket recovery,
- old ticket ids,
- private staff group support,
- importing or preserving the old queue.

## Local validation

```bash
cd apps/support-bot/upstream
uv sync --locked
uv run ruff check .
uv run mypy app/config.py app/bot/storage.py app/bot/support_context.py app/bot/support_topics.py app/bot/support_metadata.py app/bot/newsletter.py app/bot/telegram_client.py
uv run pytest
```

Docker image build:

```bash
docker build -f apps/support-bot/Dockerfile -t qpi-support-bot:local apps/support-bot
```

Local Redis helper:

```bash
docker compose -f apps/support-bot/compose.dev.yml up -d
```

Set these variables when running the bot locally:

```bash
export SUPPORT_BOT_TELEGRAM_BOT_TOKEN=<bot-token>
export SUPPORT_BOT_GROUP_ID=<topic-enabled-supergroup-id>
export SUPPORT_BOT_OWNER_ID=<telegram-id>
export SUPPORT_BOT_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/qpi
export SUPPORT_BOT_DB_SCHEMA=support_bot
export SUPPORT_BOT_REDIS_DB=7
export REDIS_HOST=127.0.0.1
export TELEGRAM_API_PROXY_URLS=http://<proxy-host>:<proxy-port>
```

## Deployment

- Production image: `apps/support-bot/Dockerfile`.
- Production compose stack: `apps/support-bot/compose.prod.yml`.
- Deploy wrapper: `scripts/deploy/support_bot.sh <image-ref>`.
- CI workflow: `.github/workflows/support_bot_ci.yml`.
- Deploy workflow: `.github/workflows/support_bot_deploy.yml`.
- Runtime VM is private-only; manual workstation deploys must set `SUPPORT_BOT_VM_SSH_PROXY_HOST=<qpi-bot-public-ip>` so SSH/scp can hop through the marketplace bot VM.
- `/opt/support-bot/current` is a deploy-managed symlink. Do not create it as a normal directory.
- Deploy archive contains `compose.prod.yml` plus `.env`; there is no rendered YAML config file.

Important deployment inputs:

- `SUPPORT_BOT_TELEGRAM_BOT_TOKEN`
- `SUPPORT_BOT_GROUP_ID`
- `SUPPORT_BOT_OWNER_ID`
- `SUPPORT_BOT_DEV_IDS` optional, falls back to owner behavior in the app
- `SUPPORT_BOT_DATABASE_URL` or `DATABASE_URL`
- `SUPPORT_BOT_DB_SCHEMA`, default `support_bot`
- `SUPPORT_BOT_REDIS_DB`, default `7`
- `TELEGRAM_API_PROXY_URLS`

Deploy smoke checks include:

- Redis PING through the compose `redis` service,
- PostgreSQL schema creation/verification through the deployed `supportbot` container,
- Telegram `getMe` through `TELEGRAM_API_PROXY_URLS`.

## Live verification

```bash
yc compute instance-group list-instances --name qpi-support-bot-ig --folder-id b1gmeblqlrrvm912n1uq
ssh -o ProxyCommand="ssh -i ~/.ssh/id_rsa -W %h:%p ubuntu@<qpi-bot-public-ip>" -i ~/.ssh/id_rsa ubuntu@<support-bot-private-ip>
readlink -f /opt/support-bot/current
systemctl is-active support-bot.service
sudo docker inspect -f '{{.Config.Image}}' current-supportbot-1
sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml exec -T redis redis-cli ping
```

The deploy wrapper already verifies PostgreSQL schema access from inside the running container. For manual debugging, inspect container logs first:

```bash
sudo docker compose --project-directory /opt/support-bot/current -f /opt/support-bot/current/compose.prod.yml logs --no-color --tail 100 supportbot
```

## Code map

- `apps/support-bot/upstream/app/config.py`: qpi env mapping.
- `apps/support-bot/upstream/app/__main__.py`: long-polling startup.
- `apps/support-bot/upstream/app/bot/storage.py`: PostgreSQL schema and repository.
- `apps/support-bot/upstream/app/bot/support_context.py`: `/start` payload parsing and metadata rendering.
- `apps/support-bot/upstream/app/bot/support_topics.py`: Support Topic service seam.
- `apps/support-bot/upstream/app/bot/support_metadata.py`: pinned metadata helpers.
- `apps/support-bot/upstream/app/bot/telegram_client.py`: aiogram Bot construction with proxy support.
