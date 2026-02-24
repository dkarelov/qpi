from __future__ import annotations

from tests.utils import reset_public_schema, run_downgrade, run_upgrade, table_exists


def test_migration_smoke_upgrade_downgrade_upgrade(test_database_url: str) -> None:
    reset_public_schema(test_database_url)

    run_upgrade(test_database_url, "head")
    assert table_exists(test_database_url, "users")
    assert table_exists(test_database_url, "listings")
    assert table_exists(test_database_url, "ledger_entries")

    run_downgrade(test_database_url, "base")
    assert not table_exists(test_database_url, "users")
    assert not table_exists(test_database_url, "listings")

    run_upgrade(test_database_url, "head")
    assert table_exists(test_database_url, "withdrawal_requests")
