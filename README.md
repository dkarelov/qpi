# QPI Phase 6 Order Tracker Baseline

This repository includes Phase 2 foundation, Phase 3 seller features, Phase 4 buyer features, Phase 5 daily report scrapper, and Phase 6 order tracker:

- async Python services (`services/bot_api`, `services/worker`, `services/daily_report_scrapper`, `services/order_tracker`),
- shared libs (`libs/config`, `libs/db`, `libs/domain`, `libs/logging`, `libs/integrations`),
- `psqldef`-based PostgreSQL schema management (`schema/schema.sql` source of truth),
- plain SQL transactional domain logic via `psycopg3`,
- seller domain + bot handlers (`libs/domain/seller.py`, `services/bot_api/seller_handlers.py`),
- buyer domain + bot handlers (`libs/domain/buyer.py`, `services/bot_api/buyer_handlers.py`),
- reservation timeout + order lifecycle processor in order-tracker (`reserved` -> `expired_2h`, pickup/return/unlock flow),
- daily report scrapper for WB `reportDetailByPeriod` ingestion (`services/daily_report_scrapper`),
- WB integrations (`libs/integrations/wb.py`, `libs/integrations/wb_reports.py`),
- integration tests for schema lifecycle, finance invariants, seller flow, buyer flow, and phase 5 report ingestion.

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
qpi-daily-report-scrapper --once
qpi-order-tracker --once
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

Daily report scrapper smoke check:

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
TOKEN_CIPHER_KEY=<cipher-key> \
python -m services.daily_report_scrapper.main --once
```

Order tracker smoke check:

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:15432/qpi \
python -m services.order_tracker.main --once
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
