# QPI Phase 2 Backend Foundation

This repository now includes the full Phase 2 backend baseline:

- async Python service skeletons (`services/bot_api`, `services/worker`),
- shared libs (`libs/config`, `libs/db`, `libs/domain`, `libs/logging`),
- Alembic-first PostgreSQL schema management,
- plain SQL transactional finance primitives via `psycopg3`,
- integration tests for migration path, reservation race safety, and ledger invariants.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
```

## Migration Commands

```bash
alembic upgrade head
alembic current
alembic history
alembic revision -m "add_new_change"
alembic downgrade -1
```

## Runtime Checks

```bash
qpi-bot-api --once
qpi-worker --once
```

## Test Commands

Integration tests require a reachable PostgreSQL database.
Set `TEST_DATABASE_URL`, then run:

```bash
pytest
```
