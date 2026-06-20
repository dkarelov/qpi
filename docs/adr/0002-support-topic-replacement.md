# ADR 0002: Support Topic Replacement Runtime

## Status

Accepted.

## Context

The previous qpi companion support bot depended on an abandoned upstream and modeled support as a private staff queue with Mongo-backed ticket state. The replacement target is `DefaultPerson/telegram-support-bot`, imported into `apps/support-bot/upstream` as a qpi-owned Python fork at upstream commit `b74e7b73107ea1f59cc05b878a488470fc84bd6b`.

Telegram forum topics are now the better operator interface for support because staff can work inside the existing supergroup with native per-user threading.

## Decision

Use a forum-topic model. The canonical support unit is **Support Topic**: one Telegram forum topic per Telegram Account in the configured support supergroup.

Render operator context in the topic title as `{Telegram Account name} · {Role topic} · {Support References}`. Do not create qpi metadata pinned messages; staff can use `/information` for on-demand Telegram identity and state details.

Run the replacement as an immediate no-migration cutover. The old runtime is not kept in coexistence, and no old queue state is imported.

Persist support state in the existing PostgreSQL cluster under an app-owned `support_bot` schema. Redis is ephemeral Redis for FSM/session state only and is capped separately in the container deployment.

Use long polling. Telegram Bot API egress must be validated through `TELEGRAM_API_PROXY_URLS` because proxy access is required for reliable operation.

Keep manual upstream updates. Future imports from `DefaultPerson/telegram-support-bot` are selected and reconciled by maintainers; qpi behavior and tests remain authoritative after the fork point.

## Consequences

The support-bot deploy workflow is Python/uv-based, builds the image in GitHub Actions, deploys to the private-only support-bot VM, and verifies Redis PING, PostgreSQL schema ownership, and Telegram `getMe` through the configured proxy.

The bot uses `SUPPORT_BOT_GROUP_ID` for the topic-enabled support supergroup. The old private staff group model is out of scope.

old Mongo data, `/open`, orphan-ticket recovery, old ticket ids, and old queue behavior are out of scope for the new runtime. If old production artifacts are needed for audit, treat them as historical backups outside the active application path.

End-user communication stays Russian. Staff commands and operational metadata can remain English.
