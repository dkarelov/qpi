from __future__ import annotations

from libs.devtools.validation_selection import resolve_validation_selection


def test_docs_only_change_resolves_no_validation_targets() -> None:
    selection = resolve_validation_selection(["AGENTS.md"])

    assert selection.selected_groups == ()
    assert selection.db_validation_mode == "none"
    assert selection.fast_pytest_targets == ()
    assert selection.db_pytest_targets == ()
    assert selection.function_targets == ()
    assert selection.has_runtime_changes is False
    assert selection.needs_private_runner is False


def test_bot_runtime_and_notifications_change_selects_targeted_runtime_and_order_tracker() -> None:
    selection = resolve_validation_selection(
        [
            "services/bot_api/telegram_runtime.py",
            "libs/domain/notifications.py",
        ]
    )

    assert selection.db_validation_mode == "targeted"
    assert "marketplace_runtime" in selection.selected_groups
    assert "order_tracker_shared" in selection.selected_groups
    assert selection.has_runtime_changes is True
    assert selection.function_targets == ("order_tracker",)
    assert "tests/test_notifications.py" in selection.db_pytest_targets
    assert "tests/test_buyer_phase4.py" in selection.db_pytest_targets
    assert "tests/test_order_tracker_phase6.py" in selection.db_pytest_targets
    assert "tests/test_telegram_runtime_ux_phase9.py" in selection.fast_pytest_targets


def test_blockchain_checker_change_targets_only_blockchain_function() -> None:
    selection = resolve_validation_selection(["services/blockchain_checker/main.py"])

    assert selection.db_validation_mode == "targeted"
    assert selection.selected_groups == ("blockchain_checker",)
    assert selection.function_targets == ("blockchain_checker",)
    assert selection.has_runtime_changes is False
    assert selection.db_pytest_targets == ("tests/test_blockchain_checker_phase8.py",)
    assert selection.fast_pytest_targets == ("tests/test_tonapi_client.py",)


def test_schema_change_escalates_to_full_validation_and_migration() -> None:
    selection = resolve_validation_selection(["schema/schema.sql"])

    assert selection.full_db_validation is True
    assert selection.db_validation_mode == "full"
    assert selection.requires_migration is True
    assert selection.has_runtime_changes is True
    assert selection.function_targets == (
        "blockchain_checker",
        "daily_report_scrapper",
        "order_tracker",
    )


def test_validation_infra_change_forces_full_validation_without_deploy_targets() -> None:
    selection = resolve_validation_selection(["scripts/dev/test.sh"])

    assert selection.full_db_validation is True
    assert selection.db_validation_mode == "full"
    assert selection.has_runtime_changes is False
    assert selection.function_targets == ()


def test_fast_test_path_is_selected_directly() -> None:
    selection = resolve_validation_selection(["tests/test_telegram_runtime_ux_phase9.py"])

    assert selection.db_validation_mode == "none"
    assert selection.fast_pytest_targets == ("tests/test_telegram_runtime_ux_phase9.py",)


def test_migration_smoke_test_path_escalates_to_full_validation() -> None:
    selection = resolve_validation_selection(["tests/test_migrations.py"])

    assert selection.full_db_validation is True
    assert selection.requires_migration is True
    assert selection.db_validation_mode == "full"
