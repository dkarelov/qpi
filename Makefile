.PHONY: migrate-plan migrate-up migrate-down migrate-export lint sync requirements test-fast test-integration test-migration-smoke test-all reset-test-db kill-stuck-tests

migrate-plan:
	uv run python -m libs.db.schema_cli plan

migrate-up:
	uv run python -m libs.db.schema_cli apply

migrate-down:
	uv run python -m libs.db.schema_cli drop

migrate-export:
	uv run python -m libs.db.schema_cli export

sync:
	uv sync --frozen --extra dev

lint:
	uv run ruff check .

requirements:
	scripts/dev/export_requirements.sh

test-fast:
	scripts/dev/test.sh fast

test-integration:
	scripts/dev/test.sh integration

test-migration-smoke:
	scripts/dev/test.sh migration-smoke

test-all:
	scripts/dev/test.sh all

reset-test-db:
	scripts/dev/reset_test_db.sh

kill-stuck-tests:
	scripts/dev/kill-stuck-tests.sh
