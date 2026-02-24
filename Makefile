.PHONY: migrate-plan migrate-up migrate-down migrate-export test lint

migrate-plan:
	python -m libs.db.schema_cli plan

migrate-up:
	python -m libs.db.schema_cli apply

migrate-down:
	python -m libs.db.schema_cli drop

migrate-export:
	python -m libs.db.schema_cli export

test:
	pytest

lint:
	ruff check .
