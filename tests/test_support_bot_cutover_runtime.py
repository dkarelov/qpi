from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_remote_rollout_stops_existing_stack_before_switching_release_symlink() -> None:
    script = _read("infra", "scripts", "remote_rollout_support_bot.sh")

    stop_index = script.index("systemctl stop support-bot.service")
    switch_index = script.rindex('sudo rm -rf "${current_link}"')

    assert stop_index < switch_index
    assert "--remove-orphans" in script
    assert "grep -qx 'redis'" in script
    assert "grep -qx 'mongodb'" not in script


def test_support_bot_deploy_deletes_old_mongo_state_after_health_smoke() -> None:
    script = _read("scripts", "deploy", "support_bot.sh")

    smoke_index = script.index('telegram_get_me_output="$(support_bot_telegram_get_me)"')
    cleanup_index = script.index("old_mongo_cleanup")

    assert smoke_index < cleanup_index
    assert "rm -rf /var/lib/support-bot/mongodb" in script
    assert "old_mongo_deleted=true" in script
    assert "old_mongo_deleted=absent" in script


def test_support_bot_runtime_preserves_pending_updates_on_startup() -> None:
    runtime = _read("apps", "support-bot", "upstream", "app", "__main__.py")

    assert "drop_pending_updates=False" in runtime
