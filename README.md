# QPI Phase 2 Backend Foundation

This repository now includes the full Phase 2 backend baseline:

- async Python service skeletons (`services/bot_api`, `services/worker`),
- shared libs (`libs/config`, `libs/db`, `libs/domain`, `libs/logging`),
- `psqldef`-based PostgreSQL schema management (`schema/schema.sql` as source of truth),
- plain SQL transactional finance primitives via `psycopg3`,
- integration tests for schema apply/drop path, reservation race safety, and ledger invariants.

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

## Test Commands

Integration tests require a reachable PostgreSQL database.
Set `TEST_DATABASE_URL`, then run:

```bash
pytest
```
