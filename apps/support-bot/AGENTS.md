# Support Bot AGENTS

Last updated: 2026-03-26 UTC

## Scope

- `apps/support-bot/upstream` is a vendored `git subtree` import of `bostrot/telegram-support-bot`.
- qpi-owned support-bot files live next to the vendored tree and must stay outside it whenever possible.
- V1 support-bot scope is Telegram-only, Russian UX, no Signal, no web chat, no LLM, no public app ports.

## Local commands

- Upgrade local NodeSource to `node_24.x` before using host Node for this app.
- Verify local prerequisites:
  - `node -v`
  - `docker compose version`
  - `mongosh --version`
- App validation commands:
  - `cd apps/support-bot/upstream && npm ci`
  - `cd apps/support-bot/upstream && npm run build`
  - `cd apps/support-bot/upstream && npm test`
- Docker image build:
  - `docker build -f apps/support-bot/Dockerfile -t qpi-support-bot:local apps/support-bot`
- Local Mongo only:
  - `docker compose -f apps/support-bot/compose.dev.yml up -d`

## Deploy

- Terraform for support-bot infra lives under `infra/support_bot*.tf`.
- The runtime deploy entrypoint is `scripts/deploy/support_bot.sh`.
- The runtime is private-only and long-polls Telegram, so there is no webhook or public listener.
- Production deploys are expected to run from the existing private runner workflow, not from the workstation.

## Upstream update policy

- Prefer overlay files over editing `apps/support-bot/upstream`.
- If an upstream patch is unavoidable, keep it minimal and document it in `apps/support-bot/README.local.md`.
- Update upstream via `git subtree pull --prefix=apps/support-bot/upstream https://github.com/bostrot/telegram-support-bot.git <ref> --squash`.

