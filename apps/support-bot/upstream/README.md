# qpi Support Bot Fork

This directory is a qpi-owned Python fork/import of
`DefaultPerson/telegram-support-bot` at subtree split
`db5edbeebec4e0ed6c553700c871d8f11c793be5`.

The active qpi runtime contract is documented in
[`../README.local.md`](../README.local.md) and [`../AGENTS.md`](../AGENTS.md).
Those qpi docs override upstream README/setup assumptions.

Current qpi runtime facts:

- Python 3.14 nested uv project.
- Telegram long polling only; no public webhook listener.
- Private-only support-bot VM.
- Persistent state in PostgreSQL schema `support_bot`.
- Redis only for ephemeral FSM/session state.
- Russian end-user private-chat UX.
- Forum-topic support model: one Support Topic per Telegram account in the
  configured support supergroup.
- Optional policy and LLM layers are disabled by default and enabled only by
  explicit environment configuration.

Local validation:

```bash
uv sync --locked
uv run ruff check .
uv run mypy app/config.py app/bot/storage.py app/bot/support_context.py app/bot/support_runtime.py app/bot/support_topics.py app/bot/newsletter.py app/bot/postgres_smoke.py app/bot/telegram_client.py
uv run pytest
```

Do not use upstream-style `pip install`, SQLite, language-selection, public
webhook, or old queue/Mongo runbooks for this fork.
