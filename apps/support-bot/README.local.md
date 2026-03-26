# qpi Support Bot Overlay

This directory contains the qpi-owned overlay around the vendored upstream Telegram support bot in `apps/support-bot/upstream`.

## Pinned upstream

- Repository: `https://github.com/bostrot/telegram-support-bot`
- Imported via `git subtree`
- Pinned upstream commit: `db5edbeebec4e0ed6c553700c871d8f11c793be5`

## Local layout

- `upstream/`: vendored upstream source tree
- `Dockerfile`: qpi production image on Node 24
- `compose.dev.yml`: local Mongo helper compose file
- `compose.prod.yml`: production compose stack for the VM
- `config/config.template.yaml`: Russian Telegram-only production config template

## Local workflow

1. Upgrade local NodeSource from `node_22.x` to `node_24.x`.
2. Copy `config/config.template.yaml` to `config/config.local.yaml`.
3. Replace the placeholder token/chat/owner values in `config/config.local.yaml`.
4. For local host-node development, set `mongodb_uri: 'mongodb://127.0.0.1:27017/support'`.
5. Start Mongo:

   ```bash
   docker compose -f apps/support-bot/compose.dev.yml up -d
   ```

6. Validate upstream app:

   ```bash
   cd apps/support-bot/upstream
   npm ci
   npm run build
   npm test
   ```

7. Run the upstream dev server:

   ```bash
   cd apps/support-bot/upstream
   npm run dev
   ```

## qpi-owned behavior

- Node 24 is the target for local dev, CI, and production image builds.
- Production deploys build the Docker image in GitHub Actions and load it on the target VM.
- V1 production stack runs only `supportbot` and `mongodb`.
- Signal, web chat, LLM, backups, and restore automation are intentionally out of scope for V1.

## Operational gotchas

- The support-bot VM is private-only. Manual workstation deploys must set `SUPPORT_BOT_VM_SSH_PROXY_HOST=<qpi-bot-public-ip>` so `scripts/deploy/support_bot.sh` can use the qpi bot VM as a bastion.
- `/opt/support-bot/current` is a deploy-managed symlink. Do not create it as a normal directory in cloud-init or by hand.
- The support-bot security group allows TCP/22 from `0.0.0.0/0` only because Yandex instance-group health checks hit SSH from outside the runner/bot security groups; the VM still has no public IP.
- Cloud-init `runcmd` sections that need `pipefail` must execute through `bash -lc`, not plain `sh`, or Docker/bootstrap installation will fail.
- A normal private Telegram group works for `staffchat_id`; a supergroup is optional, not required.
- The default qpi template currently ships with:
  - `auto_close_tickets: true`
  - `clean_replies: true`
  - no Telegram `language_code` field in the staff-facing ticket header

## Editable template surface

Primary editable template:

- `apps/support-bot/config/config.template.yaml`

Useful fields there:

- `language.startCommandText`
- `language.faqCommandText`
- `language.helpCommandText`
- `language.helpCommandStaffText`
- `language.confirmationMessage`
- `language.blockedSpam`
- `language.ticketClosed`
- `language.ticketClosedError`
- `autoreply`
- `categories`
- `clean_replies`
- `auto_close_tickets`

Reference-only upstream template:

- `apps/support-bot/upstream/config/config-sample.yaml`

Formatting logic that consumes those template values:

- staff ticket header/body: `apps/support-bot/upstream/src/users.ts`
- staff reply formatting to user: `apps/support-bot/upstream/src/staff.ts`
- `/start` and `/faq` handlers: `apps/support-bot/upstream/src/handlers.ts`
- `/help`, `/open`, `/close`, `/reopen`: `apps/support-bot/upstream/src/commands.ts`
