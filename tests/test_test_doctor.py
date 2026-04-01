from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "dev" / "test_doctor.sh"


def _write_stub(path: Path, name: str, body: str) -> None:
    target = path / name
    target.write_text(body, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)


def _base_env(tmp_path: Path) -> dict[str, str]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()

    _write_stub(stub_bin, "uv", "#!/usr/bin/env bash\nexit 0\n")
    _write_stub(stub_bin, "ssh", "#!/usr/bin/env bash\nexit 0\n")
    _write_stub(
        stub_bin,
        "rg",
        (
            "#!/usr/bin/env bash\n"
            "python3 -c 'import re, sys; "
            "pattern = sys.argv[1]; "
            "data = sys.stdin.read(); "
            "raise SystemExit(0 if re.search(pattern, data) else 1)' \"$1\"\n"
        ),
    )

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["QPI_TEST_ENV_FILE"] = str(tmp_path / ".env.test.local")
    env.pop("TEST_DATABASE_URL", None)
    return env


def test_doctor_fails_fast_when_env_file_is_missing(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    _write_stub(tmp_path / "bin", "ss", "#!/usr/bin/env bash\nexit 0\n")
    _write_stub(tmp_path / "bin", "psql", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Run: scripts/dev/write_test_env.sh --mode tunnel" in result.stderr


def test_doctor_reports_missing_local_tunnel(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    Path(env["QPI_TEST_ENV_FILE"]).write_text(
        "export TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:15432/qpi_test\n",
        encoding="utf-8",
    )
    _write_stub(tmp_path / "bin", "ss", "#!/usr/bin/env bash\nexit 0\n")
    _write_stub(tmp_path / "bin", "psql", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Local DB tunnel is missing on 127.0.0.1:15432." in result.stderr


def test_doctor_succeeds_when_tunnel_and_psql_are_available(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    Path(env["QPI_TEST_ENV_FILE"]).write_text(
        "export TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:15432/qpi_test\n",
        encoding="utf-8",
    )
    _write_stub(
        tmp_path / "bin",
        "ss",
        "#!/usr/bin/env bash\nprintf 'LISTEN 0 128 127.0.0.1:15432 0.0.0.0:*\\n'\n",
    )
    _write_stub(tmp_path / "bin", "psql", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "db_mode=tunnel" in result.stdout
    assert "doctor=ok" in result.stdout
