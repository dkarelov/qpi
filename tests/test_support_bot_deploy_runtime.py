from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_support_bot_ci_uses_uv_python_checks_and_no_node_stack() -> None:
    workflow = _read(".github", "workflows", "support_bot_ci.yml")

    assert "actions/setup-python" in workflow
    assert "uv sync --locked" in workflow
    assert "uv run ruff check ." in workflow
    assert "uv run mypy" in workflow
    assert "uv run pytest" in workflow
    assert "docker build -f apps/support-bot/Dockerfile" in workflow

    assert "actions/setup-node" not in workflow
    assert "npm " not in workflow
    assert "package-lock.json" not in workflow


def test_support_bot_deploy_workflow_uses_forum_group_db_redis_and_proxy_env() -> None:
    workflow = _read(".github", "workflows", "support_bot_deploy.yml")

    assert "actions/setup-python" in workflow
    assert "uv sync --locked" in workflow
    assert "uv run ruff check ." in workflow
    assert "uv run mypy" in workflow
    assert "uv run pytest" in workflow
    assert "SUPPORT_BOT_GROUP_ID" in workflow
    assert "SUPPORT_BOT_DATABASE_URL" in workflow
    assert "SUPPORT_BOT_DB_SCHEMA" in workflow
    assert "SUPPORT_BOT_REDIS_DB" in workflow
    assert "TELEGRAM_API_PROXY_URLS" in workflow

    assert "SUPPORT_BOT_STAFFCHAT_ID" not in workflow
    assert "actions/setup-node" not in workflow
    assert "npm " not in workflow
    assert "package-lock.json" not in workflow


def test_support_bot_compose_runs_supportbot_with_ephemeral_redis_only() -> None:
    prod = _read("apps", "support-bot", "compose.prod.yml")
    dev = _read("apps", "support-bot", "compose.dev.yml")
    compose_text = f"{prod}\n{dev}"

    assert "supportbot:" in prod
    assert "redis:" in prod
    assert "redis-server" in compose_text
    assert "--maxmemory" in compose_text
    assert "512mb" in compose_text
    assert "env_file" in prod

    assert "mongodb" not in compose_text.lower()
    assert "mongo:" not in compose_text.lower()
    assert "/var/lib/support-bot/mongodb" not in compose_text
    assert "/etc/support-bot/config.yaml" not in prod


def test_support_bot_deploy_scripts_validate_postgres_redis_and_proxy_get_me() -> None:
    deploy = _read("scripts", "deploy", "support_bot.sh")
    preflight = _read("scripts", "deploy", "preflight.sh")
    remote_rollout = _read("infra", "scripts", "remote_rollout_support_bot.sh")

    assert "SUPPORT_BOT_GROUP_ID" in deploy
    assert "SUPPORT_BOT_STAFFCHAT_ID" not in deploy
    assert "resolve_support_bot_database_url" in deploy
    assert "support_bot_telegram_get_me" in deploy
    assert "support_bot_telegram_get_chat" in deploy
    assert "support_bot_telegram_get_chat_member" in deploy
    assert "--proxy" in deploy
    assert "getChat" in deploy
    assert "getChatMember" in deploy
    assert '.result.type // "-"' in deploy
    assert '.result.is_forum // false' in deploy
    assert '.result.status // "-"' in deploy
    assert '.result.can_manage_topics // false' in deploy
    assert "redis-cli ping" in deploy
    assert "asyncpg.create_pool" in deploy
    assert "create_schema" in deploy
    assert "support_bot_postgres_ok=true" in deploy
    assert "support_bot_redis_ping" in deploy
    assert deploy.index("ssh_args=()") < deploy.index("cleanup()")

    assert "SUPPORT_BOT_GROUP_ID" in preflight
    assert "SUPPORT_BOT_STAFFCHAT_ID" not in preflight
    assert "resolve_support_bot_database_url" in preflight
    assert "support_bot_telegram_get_me" in preflight
    assert "support_bot_telegram_get_chat" in preflight
    assert "support_bot_telegram_get_chat_member" in preflight
    assert "getChat" in preflight
    assert "getChatMember" in preflight
    assert "topic-enabled supergroup" in preflight
    assert "administrator with can_manage_topics=true" in preflight
    assert 'qpi_validate_telegram_api_proxy_urls "${TELEGRAM_API_PROXY_URLS:-}" 1' in preflight

    assert "mongodb" not in remote_rollout.lower()
    assert "redis" in remote_rollout.lower()
