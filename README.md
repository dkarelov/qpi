# QPI Phase 3 Seller Baseline

This repository now includes Phase 2 foundation plus Phase 3 seller implementation:

- async Python service skeletons (`services/bot_api`, `services/worker`),
- shared libs (`libs/config`, `libs/db`, `libs/domain`, `libs/logging`),
- `psqldef`-based PostgreSQL schema management (`schema/schema.sql` as source of truth),
- plain SQL transactional finance primitives via `psycopg3`,
- seller transactional domain service (`libs/domain/seller.py`),
- seller command handlers (`services/bot_api/seller_handlers.py`),
- WB ping validation client (`libs/integrations/wb.py`),
- integration tests for schema apply/drop path, reservation race safety, ledger invariants, and seller Phase 3 flows.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
```

## Migration Commands

```bash
export DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi
make migrate-plan
make migrate-up
make migrate-down
make migrate-export
```

## Runtime Checks

```bash
qpi-bot-api --once
qpi-worker --once
```

Seller command smoke check:

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
python -m services.bot_api.main --seller-command "/start" --telegram-id 1001 --telegram-username seller
```

## Test Commands

Integration tests require a reachable PostgreSQL database.
Set `TEST_DATABASE_URL`, then run:

```bash
pytest
```
