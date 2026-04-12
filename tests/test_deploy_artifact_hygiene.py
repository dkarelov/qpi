from __future__ import annotations

import json
import subprocess
import tarfile
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
    assert "Generated function requirements contain secret markers" in script
