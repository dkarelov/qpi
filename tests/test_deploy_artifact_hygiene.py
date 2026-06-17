from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
