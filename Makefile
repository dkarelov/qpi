.PHONY: migrate-up migrate-down migrate-current test lint

migrate-up:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

migrate-current:
	alembic current

test:
	pytest

lint:
	ruff check .
