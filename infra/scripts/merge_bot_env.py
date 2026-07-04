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


def merge_env(
    base: dict[str, str],
    overrides: dict[str, str],
    *,
    blank: list[str] | None = None,
    delete: list[str] | None = None,
    require_nonempty: list[str] | None = None,
) -> dict[str, str]:
    values = dict(base)
    for key, value in overrides.items():
        if value == "" and key in values:
            # An empty override preserves the existing value; use --blank to clear intentionally.
            continue
        values[key] = value
    for key in blank or []:
        values[key] = ""
    for key in delete or []:
        values.pop(key, None)

    missing = [key for key in require_nonempty or [] if not values.get(key)]
    if missing:
        raise SystemExit(f"required env keys are missing or empty after merge: {', '.join(sorted(missing))}")

    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge env overrides into base bot env file")
    parser.add_argument("--base", required=True, help="Base env file path")
    parser.add_argument("--overrides", required=True, help="Overrides env file path")
    parser.add_argument("--blank", action="append", default=[], help="Env key to set to an empty value intentionally")
    parser.add_argument("--delete", action="append", default=[], help="Env key to remove after applying overrides")
    parser.add_argument(
        "--require-nonempty",
        action="append",
        default=[],
        help="Env key that must be non-empty after the merge; fail otherwise",
    )
    args = parser.parse_args()

    base_path = Path(args.base)
    overrides_path = Path(args.overrides)

    if not base_path.exists():
        raise SystemExit(f"base env file is missing: {base_path}")
    if not overrides_path.exists():
        raise SystemExit(f"overrides env file is missing: {overrides_path}")

    values = merge_env(
        _read_env(base_path),
        _read_env(overrides_path),
        blank=args.blank,
        delete=args.delete,
        require_nonempty=args.require_nonempty,
    )
    content = "\n".join(f"{key}={values[key]}" for key in sorted(values)) + "\n"
    base_path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
