# QPI Phase 4 Buyer Baseline

This repository includes Phase 2 foundation, Phase 3 seller features, and Phase 4 buyer features:

- async Python services (`services/bot_api`, `services/worker`),
- shared libs (`libs/config`, `libs/db`, `libs/domain`, `libs/logging`, `libs/integrations`),
- `psqldef`-based PostgreSQL schema management (`schema/schema.sql` source of truth),
- plain SQL transactional domain logic via `psycopg3`,
- seller domain + bot handlers (`libs/domain/seller.py`, `services/bot_api/seller_handlers.py`),
- buyer domain + bot handlers (`libs/domain/buyer.py`, `services/bot_api/buyer_handlers.py`),
- reservation timeout processor in worker (`reserved` -> `expired_2h`),
- WB ping validation client (`libs/integrations/wb.py`),
- integration tests for schema lifecycle, finance invariants, seller flow, and buyer flow.

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

Buyer command smoke check:

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
python -m services.bot_api.main --buyer-command "/start" --telegram-id 2001 --telegram-username buyer
```

## Test Commands

Integration tests require a reachable dedicated test database.
Safety rules:

- `TEST_DATABASE_URL` database name must contain `test`,
- migration smoke tests are destructive and require disposable DB name containing
  `test` plus one of `scratch|tmp|disposable`.

Main integration suite (non-destructive schema lifecycle; truncates data in test DB per test):

```bash
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test \
pytest -q -m "not migration_smoke"
```

Migration smoke suite (destructive `apply/drop/apply`, opt-in):

```bash
RUN_MIGRATION_SMOKE=1 \
TEST_DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi_test_scratch \
pytest -q -m migration_smoke
```
