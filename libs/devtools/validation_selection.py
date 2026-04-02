from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationGroup:
    name: str
    trigger_globs: tuple[str, ...]
    fast_pytest_targets: tuple[str, ...]
    db_pytest_targets: tuple[str, ...]
    full_db_validation: bool
    requires_migration: bool
    has_runtime_changes: bool
    function_targets: tuple[str, ...]


@dataclass(frozen=True)
class ValidationSelection:
    selected_groups: tuple[str, ...]
    fast_pytest_targets: tuple[str, ...]
    db_pytest_targets: tuple[str, ...]
    full_db_validation: bool
    db_validation_mode: str
    requires_migration: bool
    has_runtime_changes: bool
    function_targets: tuple[str, ...]
    has_function_targets: bool
    needs_db_validation: bool
    needs_private_runner: bool


_DB_MANIFEST_FILES = {
    "integration": "tests/db_integration_manifest.txt",
    "schema_compat": "tests/schema_compat_manifest.txt",
    "migration_smoke": "tests/migration_smoke_manifest.txt",
}


def _repo_root(value: str | Path | None) -> Path:
    if value is None:
        return Path(__file__).resolve().parents[2]
    return Path(value).resolve()


def _normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _load_json(repo_root: Path) -> dict:
    path = repo_root / "scripts" / "dev" / "validation_groups.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_validation_groups(*, repo_root: str | Path | None = None) -> tuple[ValidationGroup, ...]:
    root = _repo_root(repo_root)
    payload = _load_json(root)
    groups: list[ValidationGroup] = []
    for item in payload.get("groups", []):
        groups.append(
            ValidationGroup(
                name=str(item["name"]),
                trigger_globs=tuple(_normalize_path(value) for value in item.get("trigger_globs", [])),
                fast_pytest_targets=tuple(
                    _normalize_path(value) for value in item.get("fast_pytest_targets", [])
                ),
                db_pytest_targets=tuple(
                    _normalize_path(value) for value in item.get("db_pytest_targets", [])
                ),
                full_db_validation=bool(item.get("full_db_validation", False)),
                requires_migration=bool(item.get("requires_migration", False)),
                has_runtime_changes=bool(item.get("has_runtime_changes", False)),
                function_targets=tuple(item.get("function_targets", [])),
            )
        )
    return tuple(groups)


def load_db_manifest_membership(
    *, repo_root: str | Path | None = None
) -> dict[str, set[str]]:
    root = _repo_root(repo_root)
    membership: dict[str, set[str]] = {name: set() for name in _DB_MANIFEST_FILES}
    for kind, relative_path in _DB_MANIFEST_FILES.items():
        path = root / relative_path
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                membership[kind].add(_normalize_path(line))
    return membership


def resolve_validation_selection(
    changed_paths: list[str] | tuple[str, ...],
    *,
    repo_root: str | Path | None = None,
) -> ValidationSelection:
    root = _repo_root(repo_root)
    groups = load_validation_groups(repo_root=root)
    membership = load_db_manifest_membership(repo_root=root)

    normalized_paths = sorted(
        {
            _normalize_path(path)
            for path in changed_paths
            if _normalize_path(path)
        }
    )

    selected_group_names: list[str] = []
    fast_targets: set[str] = set()
    db_targets: set[str] = set()
    function_targets: set[str] = set()
    full_db_validation = False
    requires_migration = False
    has_runtime_changes = False

    for group in groups:
        if not any(
            fnmatch.fnmatch(path, pattern)
            for path in normalized_paths
            for pattern in group.trigger_globs
        ):
            continue
        selected_group_names.append(group.name)
        fast_targets.update(group.fast_pytest_targets)
        db_targets.update(group.db_pytest_targets)
        function_targets.update(group.function_targets)
        full_db_validation = full_db_validation or group.full_db_validation
        requires_migration = requires_migration or group.requires_migration
        has_runtime_changes = has_runtime_changes or group.has_runtime_changes

    migration_tests = membership["migration_smoke"]
    db_manifest_tests = membership["integration"] | membership["schema_compat"] | migration_tests

    for path in normalized_paths:
        if not path.startswith("tests/test_") or not path.endswith(".py"):
            continue
        if path in migration_tests:
            full_db_validation = True
            requires_migration = True
            continue
        if path in db_manifest_tests:
            db_targets.add(path)
        else:
            fast_targets.add(path)

    db_validation_mode = "full" if full_db_validation else ("targeted" if db_targets else "none")
    needs_db_validation = db_validation_mode != "none"
    has_function_targets = bool(function_targets)
    needs_private_runner = needs_db_validation

    return ValidationSelection(
        selected_groups=tuple(sorted(selected_group_names)),
        fast_pytest_targets=tuple(sorted(fast_targets)),
        db_pytest_targets=tuple(sorted(db_targets)),
        full_db_validation=full_db_validation,
        db_validation_mode=db_validation_mode,
        requires_migration=requires_migration,
        has_runtime_changes=has_runtime_changes,
        function_targets=tuple(sorted(function_targets)),
        has_function_targets=has_function_targets,
        needs_db_validation=needs_db_validation,
        needs_private_runner=needs_private_runner,
    )


def _shell_bool(value: bool) -> str:
    return "true" if value else "false"


def _shell_join(values: tuple[str, ...]) -> str:
    return " ".join(values)


def _selection_to_shell(selection: ValidationSelection) -> str:
    assignments = {
        "selected_groups": _shell_join(selection.selected_groups),
        "fast_pytest_targets": _shell_join(selection.fast_pytest_targets),
        "db_pytest_targets": _shell_join(selection.db_pytest_targets),
        "full_db_validation": _shell_bool(selection.full_db_validation),
        "db_validation_mode": selection.db_validation_mode,
        "requires_migration": _shell_bool(selection.requires_migration),
        "has_runtime_changes": _shell_bool(selection.has_runtime_changes),
        "function_targets": _shell_join(selection.function_targets),
        "has_function_targets": _shell_bool(selection.has_function_targets),
        "needs_db_validation": _shell_bool(selection.needs_db_validation),
        "needs_private_runner": _shell_bool(selection.needs_private_runner),
    }
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in assignments.items())


def _selection_to_json(selection: ValidationSelection) -> str:
    payload = {
        "selected_groups": list(selection.selected_groups),
        "fast_pytest_targets": list(selection.fast_pytest_targets),
        "db_pytest_targets": list(selection.db_pytest_targets),
        "full_db_validation": selection.full_db_validation,
        "db_validation_mode": selection.db_validation_mode,
        "requires_migration": selection.requires_migration,
        "has_runtime_changes": selection.has_runtime_changes,
        "function_targets": list(selection.function_targets),
        "has_function_targets": selection.has_function_targets,
        "needs_db_validation": selection.needs_db_validation,
        "needs_private_runner": selection.needs_private_runner,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve validation targets for changed paths.")
    parser.add_argument("--repo-root", default=os.getcwd(), help="repository root")
    parser.add_argument("--format", choices=("shell", "json"), default="shell")
    parser.add_argument("--paths", nargs="+", required=True, help="changed relative paths")
    args = parser.parse_args()

    selection = resolve_validation_selection(args.paths, repo_root=args.repo_root)
    if args.format == "json":
        print(_selection_to_json(selection))
        return
    print(_selection_to_shell(selection))


if __name__ == "__main__":
    main()
