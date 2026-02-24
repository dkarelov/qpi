from __future__ import annotations

from tests.utils import reset_public_schema, run_schema_apply, run_schema_drop, table_exists


def test_migration_smoke_apply_drop_apply(test_database_url: str) -> None:
    reset_public_schema(test_database_url)

    run_schema_apply(test_database_url)
    run_schema_apply(test_database_url)
    assert table_exists(test_database_url, "users")
    assert table_exists(test_database_url, "listings")
    assert table_exists(test_database_url, "ledger_entries")

    run_schema_drop(test_database_url)
    assert not table_exists(test_database_url, "users")
    assert not table_exists(test_database_url, "listings")

    run_schema_apply(test_database_url)
    assert table_exists(test_database_url, "withdrawal_requests")
