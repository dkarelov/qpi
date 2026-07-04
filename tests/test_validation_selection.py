from __future__ import annotations

from libs.devtools.validation_selection import resolve_support_bot_deploy_selection, resolve_validation_selection


def test_docs_only_change_resolves_no_validation_targets() -> None:
    selection = resolve_validation_selection(["AGENTS.md"])

    assert selection.selected_groups == ()
    assert selection.db_validation_mode == "none"
    assert selection.fast_pytest_targets == ()
    assert selection.db_pytest_targets == ()
    assert selection.function_targets == ()
    assert selection.has_runtime_changes is False
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is False
    assert selection.schema_action == "none"
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
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is True
    assert selection.schema_action == "assert-clean"
    assert selection.function_targets == ("order_tracker",)
    assert "tests/test_notifications.py" in selection.db_pytest_targets
    assert "tests/test_buyer_phase4.py" in selection.db_pytest_targets
    assert "tests/test_order_tracker_phase6.py" in selection.db_pytest_targets
    assert "tests/test_telegram_runtime_ux_phase9.py" in selection.fast_pytest_targets
    assert selection.needs_private_runner is True


def test_runtime_only_notification_renderer_change_stays_runtime_only() -> None:
    selection = resolve_validation_selection(["services/bot_api/telegram_notifications.py"])

    assert selection.selected_groups == ("marketplace_runtime",)
    assert selection.db_validation_mode == "targeted"
    assert selection.has_runtime_changes is True
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is True
    assert selection.schema_action == "assert-clean"
    assert selection.function_targets == ()
    assert "tests/test_notifications.py" in selection.db_pytest_targets
    assert "tests/test_phase10_e2e_harness.py" in selection.fast_pytest_targets
    assert selection.needs_private_runner is True


def test_monitoring_integration_change_deploys_runtime() -> None:
    selection = resolve_validation_selection(["libs/integrations/yandex_monitoring.py"])

    assert selection.selected_groups == ("marketplace_runtime",)
    assert selection.has_runtime_changes is True
    assert selection.requires_schema_assert is True
    assert selection.function_targets == ()
    assert "tests/test_telegram_proxy_request.py" in selection.fast_pytest_targets
    assert "tests/test_yandex_monitoring.py" in selection.fast_pytest_targets


def test_blockchain_checker_change_targets_only_blockchain_function() -> None:
    selection = resolve_validation_selection(["services/blockchain_checker/main.py"])

    assert selection.db_validation_mode == "targeted"
    assert selection.selected_groups == ("blockchain_checker",)
    assert selection.function_targets == ("blockchain_checker",)
    assert selection.has_runtime_changes is False
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is True
    assert selection.schema_action == "assert-clean"
    assert selection.db_pytest_targets == ("tests/test_blockchain_checker_phase8.py",)
    assert selection.fast_pytest_targets == ("tests/test_tonapi_client.py",)
    assert selection.needs_private_runner is True


def test_schema_change_escalates_to_full_validation_and_migration() -> None:
    selection = resolve_validation_selection(["schema/schema.sql"])

    assert selection.full_db_validation is True
    assert selection.db_validation_mode == "full"
    assert selection.requires_migration is True
    assert selection.requires_schema_apply is True
    assert selection.requires_schema_assert is False
    assert selection.schema_action == "apply"
    assert selection.has_runtime_changes is False
    assert selection.function_targets == ()
    assert selection.needs_private_runner is True


def test_validation_infra_change_forces_full_validation_without_deploy_targets() -> None:
    selection = resolve_validation_selection(["scripts/dev/test.sh"])

    assert selection.full_db_validation is True
    assert selection.db_validation_mode == "full"
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is False
    assert selection.schema_action == "none"
    assert selection.has_runtime_changes is False
    assert selection.function_targets == ()
    assert selection.needs_private_runner is True


def test_workflow_change_resolves_fast_only_validation() -> None:
    selection = resolve_validation_selection([".github/workflows/post_merge.yml"])

    assert selection.selected_groups == ("validation_infra_fast",)
    assert selection.full_db_validation is False
    assert selection.db_validation_mode == "none"
    assert selection.fast_pytest_targets == (
        "tests/test_test_doctor.py",
        "tests/test_validation_selection.py",
    )
    assert selection.db_pytest_targets == ()
    assert selection.has_runtime_changes is False
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is False
    assert selection.schema_action == "none"
    assert selection.function_targets == ()
    assert selection.needs_private_runner is False


def test_fast_test_path_is_selected_directly() -> None:
    selection = resolve_validation_selection(["tests/test_telegram_runtime_ux_phase9.py"])

    assert selection.db_validation_mode == "none"
    assert selection.fast_pytest_targets == ("tests/test_telegram_runtime_ux_phase9.py",)
    assert selection.schema_action == "none"
    assert selection.needs_private_runner is False


def test_migration_smoke_test_path_escalates_to_full_validation() -> None:
    selection = resolve_validation_selection(["tests/test_migrations.py"])

    assert selection.full_db_validation is True
    assert selection.requires_migration is True
    assert selection.requires_schema_apply is False
    assert selection.requires_schema_assert is False
    assert selection.schema_action == "none"
    assert selection.db_validation_mode == "full"
    assert selection.needs_private_runner is True


def test_schema_tooling_and_schema_compat_tests_do_not_select_service_deploys() -> None:
    selection = resolve_validation_selection(
        [
            "AGENTS.md",
            "libs/db/psqldef.py",
            "tests/test_runtime_schema_compatibility.py",
        ]
    )

    assert selection.db_validation_mode == "full"
    assert selection.requires_migration is True
    assert selection.requires_schema_apply is True
    assert selection.schema_action == "apply"
    assert selection.has_runtime_changes is False
    assert selection.function_targets == ()
    assert selection.needs_private_runner is True


def test_bot_api_change_deploys_runtime_only() -> None:
    selection = resolve_validation_selection(["services/bot_api/telegram_runtime.py"])

    assert selection.selected_groups == ("marketplace_runtime",)
    assert selection.has_runtime_changes is True
    assert selection.function_targets == ()
    assert selection.requires_schema_assert is True
    assert selection.schema_action == "assert-clean"


def test_order_tracker_service_change_deploys_only_order_tracker() -> None:
    selection = resolve_validation_selection(["services/order_tracker/main.py"])

    assert selection.selected_groups == ("order_tracker",)
    assert selection.has_runtime_changes is False
    assert selection.function_targets == ("order_tracker",)
    assert selection.schema_action == "assert-clean"


def test_support_bot_docs_and_tests_do_not_deploy_image() -> None:
    selection = resolve_support_bot_deploy_selection(
        [
            "apps/support-bot/README.local.md",
            "apps/support-bot/upstream/tests/qpi/test_runtime_bootstrap.py",
        ]
    )

    assert selection.needs_validation is True
    assert selection.needs_image_deploy is False


def test_support_bot_runtime_change_deploys_image() -> None:
    selection = resolve_support_bot_deploy_selection(["apps/support-bot/upstream/app/bot/manager.py"])

    assert selection.needs_validation is True
    assert selection.needs_image_deploy is True


def test_presentation_only_change_routes_to_hosted_lane() -> None:
    selection = resolve_validation_selection(
        [
            "services/bot_api/presentation.py",
            "services/bot_api/buyer_listing_copy.py",
        ]
    )

    assert selection.selected_groups == ("marketplace_presentation",)
    assert selection.has_runtime_changes is True
    assert selection.db_validation_mode == "none"
    assert selection.db_pytest_targets == ()
    assert "tests/test_presentation.py" in selection.fast_pytest_targets
    assert selection.requires_schema_assert is True
    assert selection.schema_action == "assert-clean"
    assert selection.needs_private_runner is False
    assert selection.deploy_lane == "hosted"


def test_presentation_change_mixed_with_flow_change_still_selects_runtime_group() -> None:
    selection = resolve_validation_selection(
        [
            "services/bot_api/presentation.py",
            "services/bot_api/buyer_marketplace_flow.py",
        ]
    )

    assert "marketplace_presentation" in selection.selected_groups
    assert "marketplace_runtime" in selection.selected_groups
    assert selection.db_validation_mode == "targeted"
    assert selection.needs_private_runner is True
    assert selection.deploy_lane == "private"


def test_runtime_flow_change_with_db_targets_routes_to_private_lane() -> None:
    selection = resolve_validation_selection(["services/bot_api/telegram_runtime.py"])

    assert selection.needs_private_runner is True
    assert selection.deploy_lane == "private"


def test_schema_change_routes_to_private_lane() -> None:
    selection = resolve_validation_selection(["schema/schema.sql"])

    assert selection.needs_private_runner is True
    assert selection.deploy_lane == "private"


def test_docs_only_change_routes_to_no_deploy_lane() -> None:
    selection = resolve_validation_selection(["AGENTS.md"])

    assert selection.needs_private_runner is False
    assert selection.deploy_lane == "none"


def test_function_change_with_db_targets_routes_to_private_lane() -> None:
    selection = resolve_validation_selection(["services/order_tracker/main.py"])

    assert selection.has_function_targets is True
    assert selection.db_validation_mode == "targeted"
    assert selection.needs_private_runner is True
    assert selection.deploy_lane == "private"
