from __future__ import annotations

import argparse
from pathlib import Path


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge env overrides into base bot env file")
    parser.add_argument("--base", required=True, help="Base env file path")
    parser.add_argument("--overrides", required=True, help="Overrides env file path")
    args = parser.parse_args()

    base_path = Path(args.base)
    overrides_path = Path(args.overrides)

    if not base_path.exists():
        raise SystemExit(f"base env file is missing: {base_path}")
    if not overrides_path.exists():
        raise SystemExit(f"overrides env file is missing: {overrides_path}")

    values = _read_env(base_path)
    values.update(_read_env(overrides_path))
    content = "\n".join(f"{key}={values[key]}" for key in sorted(values)) + "\n"
    base_path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
