from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "infra" / "scripts" / "merge_bot_env.py"
_spec = importlib.util.spec_from_file_location("merge_bot_env", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
merge_bot_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_bot_env)


def test_override_replaces_base_value() -> None:
    merged = merge_bot_env.merge_env({"A": "old", "B": "keep"}, {"A": "new"})

    assert merged == {"A": "new", "B": "keep"}


def test_empty_override_preserves_existing_base_value() -> None:
    merged = merge_bot_env.merge_env({"SUPPORT_BOT_USERNAME": "support_bot"}, {"SUPPORT_BOT_USERNAME": ""})

    assert merged == {"SUPPORT_BOT_USERNAME": "support_bot"}


def test_empty_override_for_unknown_key_is_added_as_empty() -> None:
    merged = merge_bot_env.merge_env({"A": "1"}, {"NEW_KEY": ""})

    assert merged == {"A": "1", "NEW_KEY": ""}


def test_blank_clears_value_intentionally() -> None:
    merged = merge_bot_env.merge_env({"A": "1"}, {"A": ""}, blank=["A"])

    assert merged == {"A": ""}


def test_delete_removes_key() -> None:
    merged = merge_bot_env.merge_env({"A": "1", "LEGACY": "x"}, {}, delete=["LEGACY"])

    assert merged == {"A": "1"}


def test_require_nonempty_passes_for_populated_keys() -> None:
    merged = merge_bot_env.merge_env({"TOKEN": "abc"}, {}, require_nonempty=["TOKEN"])

    assert merged == {"TOKEN": "abc"}


def test_require_nonempty_fails_for_missing_key() -> None:
    with pytest.raises(SystemExit, match="TOKEN"):
        merge_bot_env.merge_env({}, {}, require_nonempty=["TOKEN"])


def test_require_nonempty_fails_for_blanked_key() -> None:
    with pytest.raises(SystemExit, match="TOKEN"):
        merge_bot_env.merge_env({"TOKEN": "abc"}, {}, blank=["TOKEN"], require_nonempty=["TOKEN"])
