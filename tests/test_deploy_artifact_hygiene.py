from __future__ import annotations

import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _detect_ci_changes(*args: str) -> dict[str, str]:
    result = subprocess.run(
        ["scripts/common/detect_ci_changes.sh", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        fields = shlex.split(raw_line)
        assert len(fields) == 1
        key, value = fields[0].split("=", 1)
        output[key] = value
    return output


def test_runtime_metadata_archive_excludes_ignored_local_secret_files() -> None:
    result = subprocess.run(
        ["scripts/deploy/runtime.sh", "metadata"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(result.stdout)
    archive_path = Path(metadata["archive_path"])

    with tarfile.open(archive_path, "r:gz") as archive:
        names = {name.removeprefix("./") for name in archive.getnames()}

    assert "infra/terraform.tfvars" not in names
    assert ".env" not in names
    assert ".env.test.local" not in names
    assert not any(name.startswith(".artifacts/") for name in names)


def test_function_bundle_script_does_not_write_tokenized_requirements_into_stage() -> None:
    script = (REPO_ROOT / "scripts/deploy/function.sh").read_text(encoding="utf-8")

    assert "x-access-token:${GH_TOKEN}" not in script
    assert "--find-links ./vendor/wheels" in script
    assert '"--no-index"' not in script
    assert "Generated function requirements contain secret markers" in script
    assert "GIT_CONFIG_GLOBAL" in script


def test_runtime_deploy_proxy_urls_override_is_required_and_legacy_key_is_deleted() -> None:
    script = (REPO_ROOT / "scripts/deploy/runtime.sh").read_text(encoding="utf-8")

    assert 'qpi_require_env "TELEGRAM_API_PROXY_URLS"' in script
    assert "TELEGRAM_API_PROXY_URLS=${TELEGRAM_API_PROXY_URLS}" in script
    assert "--delete TELEGRAM_API_PROXY_URL" in script
    assert "TELEGRAM_API_PROXY_URL=${TELEGRAM_API_PROXY_URL:-}" not in script


def test_deploy_scripts_validate_telegram_proxy_and_support_explicit_bypass() -> None:
    common_script = (REPO_ROOT / "scripts/deploy/common.sh").read_text(encoding="utf-8")
    preflight_script = (REPO_ROOT / "scripts/deploy/preflight.sh").read_text(encoding="utf-8")
    runtime_script = (REPO_ROOT / "scripts/deploy/runtime.sh").read_text(encoding="utf-8")
    remote_rollout_script = (REPO_ROOT / "infra/scripts/remote_rollout_bot.sh").read_text(encoding="utf-8")

    assert "qpi_reject_legacy_telegram_api_proxy_url" in common_script
    assert "qpi_validate_telegram_api_proxy_urls" in common_script
    assert 'qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 2' in preflight_script
    assert 'qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 2' in runtime_script
    assert "validate_telegram_api_proxy_urls" in remote_rollout_script
    assert 'for _round in 1 2 3; do' in preflight_script
    assert 'for _round in 1 2 3; do' in remote_rollout_script
    assert "QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE" in preflight_script
    assert "QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE" in runtime_script
    assert "QPI_ALLOW_DEPLOY_WHEN_TELEGRAM_UNREACHABLE" in remote_rollout_script


def test_runtime_workflows_use_proxy_urls_secret() -> None:
    workflow_text = "\n".join(
        [
            (REPO_ROOT / ".github/workflows/deploy_runtime.yml").read_text(encoding="utf-8"),
            (REPO_ROOT / ".github/workflows/post_merge.yml").read_text(encoding="utf-8"),
        ]
    )

    assert "secrets.TELEGRAM_API_PROXY_URLS" in workflow_text
    assert "secrets.TELEGRAM_API_PROXY_URL }}" not in workflow_text


def test_post_merge_splits_schema_sync_from_service_rollout_and_parallelizes_functions() -> None:
    workflow = (REPO_ROOT / ".github/workflows/post_merge.yml").read_text(encoding="utf-8")

    assert "requires_schema_apply" in workflow
    assert "requires_schema_assert" in workflow
    assert "schema_action" in workflow
    assert "schema-sync:" in workflow
    assert "scripts/deploy/schema_remote.sh apply" in workflow
    assert "scripts/deploy/schema_remote.sh assert-clean" in workflow
    assert "scripts/deploy/preflight.sh runtime --skip-schema-check" in workflow
    assert "scripts/deploy/preflight.sh functions --skip-schema-check" in workflow
    assert "QPI_DEPLOY_SCHEMA_MODE: never" in workflow
    assert "QPI_SKIP_FUNCTION_SCHEMA_CHECK" in workflow
    assert "deploy-functions-after-runtime:" in workflow
    assert "needs.predeploy-marketplace.outputs.runtime_schema_action == 'apply'" in workflow
    assert 'pids+=("$!")' in workflow

    deploy_functions_start = workflow.index("  deploy-functions:")
    deploy_functions_after_runtime_start = workflow.index("  deploy-functions-after-runtime:")
    deploy_functions_block = workflow[deploy_functions_start:deploy_functions_after_runtime_start]

    assert "- deploy-runtime" not in deploy_functions_block


def test_detect_ci_changes_indeterminate_diff_falls_back_to_full_marketplace_deploy() -> None:
    output = _detect_ci_changes(
        "--event-name",
        "push",
        "--base-sha",
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "--head-sha",
        "HEAD",
    )

    assert output["needs_db_validation"] == "true"
    assert output["requires_schema_apply"] == "true"
    assert output["requires_schema_assert"] == "false"
    assert output["schema_action"] == "apply"
    assert output["requires_migration"] == "true"
    assert output["has_runtime_changes"] == "true"
    assert output["function_targets"].split() == [
        "daily_report_scrapper",
        "order_tracker",
        "blockchain_checker",
    ]
    assert output["has_function_targets"] == "true"
    assert output["needs_private_runner"] == "true"
    assert output["db_validation_mode"] == "full"
    assert output["db_validation_targets"] == ""


def test_detect_ci_changes_force_full_validation_does_not_invent_deploy_targets_on_empty_diff() -> None:
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    output = _detect_ci_changes(
        "--event-name",
        "workflow_dispatch",
        "--base-sha",
        head_sha,
        "--head-sha",
        head_sha,
        "--force-full-validation",
    )

    assert output["needs_db_validation"] == "true"
    assert output["requires_schema_apply"] == "false"
    assert output["requires_schema_assert"] == "false"
    assert output["schema_action"] == "none"
    assert output["requires_migration"] == "true"
    assert output["has_runtime_changes"] == "false"
    assert output["function_targets"] == ""
    assert output["has_function_targets"] == "false"
    assert output["needs_private_runner"] == "true"
    assert output["db_validation_mode"] == "full"
    assert output["db_validation_targets"] == ""


def test_private_git_auth_helper_can_use_scoped_git_config() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        scoped_git_config = tmp_path / "gitconfig"
        env = {
            **os.environ,
            "GH_TOKEN": "test-token",
            "HOME": str(home_dir),
            "GIT_CONFIG_GLOBAL": str(scoped_git_config),
        }

        subprocess.run(
            ["scripts/common/setup_private_git_auth.sh"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        assert scoped_git_config.is_file()
        assert not (home_dir / ".gitconfig").exists()
        assert 'url "https://x-access-token:test-token@github.com/"' in scoped_git_config.read_text(
            encoding="utf-8"
        )


def test_private_runner_has_warm_online_fast_path_after_shutdown_safeguards() -> None:
    script = (REPO_ROOT / "scripts/deploy/private_runner.sh").read_text(encoding="utf-8")

    assert "runner_service_active()" in script
    assert "warm_runner_ready()" in script
    assert "PRIVATE_RUNNER_FORCE_RECONFIGURE" in script
    assert "runner_exists || return 1" in script
    assert '[[ "$(runner_online)" == "1" ]] || return 1' in script
    assert "runner_service_active || return 1" in script
    assert 'sudo sh -c \'tr -d "\\r\\n" < "$1"\'' in script
    assert 'runner_unit="${runner_unit##*/}"' in script
    assert "skipped runner reconfiguration" in script

    ensure_start = script.index("  ensure-ready)")
    ensure_end = script.index("    ;;\n  schedule-stop)", ensure_start)
    ensure_block = script[ensure_start:ensure_end]

    assert ensure_block.index("install_or_refresh_autoshutdown_controller") < ensure_block.index(
        "warm_runner_ready"
    )
    assert ensure_block.index("autoshutdown_heartbeat") < ensure_block.index("warm_runner_ready")
    assert ensure_block.index('schedule_shutdown "${PRIVATE_RUNNER_MAX_SESSION_MINUTES}"') < ensure_block.index(
        "warm_runner_ready"
    )
    assert ensure_block.index("warm_runner_ready") < ensure_block.index("install_or_reconfigure_runner")
