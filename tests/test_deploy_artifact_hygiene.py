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
