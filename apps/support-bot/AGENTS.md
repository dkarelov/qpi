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
- Manual workstation deploys to the private-only VM must set `SUPPORT_BOT_VM_SSH_PROXY_HOST=<qpi-bot-public-ip>` so SSH/scp can hop through the always-on qpi bot VM.
- `/opt/support-bot/current` is a symlink managed by the deploy wrapper; it must never be pre-created as a real directory.
- The support-bot security group intentionally allows TCP/22 from `0.0.0.0/0` for instance-group health checks, but the VM still has no public IP, so there is no direct public SSH path.

## Runtime defaults

- `clean_replies: true` is the qpi default, so staff replies are sent as plain message text without greeting/signature wrappers.
- `auto_close_tickets: true` is the qpi default, so a successful staff reply removes the ticket from `/open`.
- Ticket headers sent to staff do not include Telegram `language_code`; that field was removed because it reflects Telegram client metadata, not the actual message language.
- A normal private Telegram group works for `staffchat_id`; a supergroup is not required for the current Telegram-only flow.
- The editable runtime template is `apps/support-bot/config/config.template.yaml`; the rendered production copy lives on the VM at `/etc/support-bot/config.yaml`.

## Upstream update policy

- Prefer overlay files over editing `apps/support-bot/upstream`.
- If an upstream patch is unavoidable, keep it minimal and document it in `apps/support-bot/README.local.md`.
- Update upstream via `git subtree pull --prefix=apps/support-bot/upstream https://github.com/bostrot/telegram-support-bot.git <ref> --squash`.
