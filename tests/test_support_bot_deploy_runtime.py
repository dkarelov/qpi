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

    assert "determine-changes:" in workflow
    assert "--selector support-bot" in workflow
    assert "validate-support-bot:" in workflow
    assert "resolve-image-metadata:" in workflow
    assert "support_bot_needs_image_deploy" in workflow
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


def test_support_bot_deploy_starts_runner_after_metadata_not_after_image_build() -> None:
    workflow = _read(".github", "workflows", "support_bot_deploy.yml")

    start_runner = workflow[workflow.index("  start-private-runner:") : workflow.index("  predeploy-support-bot:")]
    assert "- resolve-image-metadata" in start_runner
    assert "- build-image" not in start_runner
    assert "needs.resolve-image-metadata.outputs.registry_present == 'true'" in start_runner

    build_image = workflow[workflow.index("  build-image:") : workflow.index("  start-private-runner:")]
    assert "- validate-support-bot" in build_image
    assert "- resolve-image-metadata" in build_image
    assert "docker build -f apps/support-bot/Dockerfile" in build_image


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
    postgres_smoke = _read("apps", "support-bot", "upstream", "app", "bot", "postgres_smoke.py")

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
    assert "python -m app.bot.postgres_smoke" in deploy
    assert "support_bot_redis_ping" in deploy
    assert deploy.index("ssh_args=()") < deploy.index("cleanup()")

    assert "asyncpg.create_pool" in postgres_smoke
    assert "create_schema" in postgres_smoke
    assert "support_bot_postgres_ok=true" in postgres_smoke

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


def test_support_bot_deploy_archive_does_not_retain_secret_env() -> None:
    deploy = _read("scripts", "deploy", "support_bot.sh")
    remote_rollout = _read("infra", "scripts", "remote_rollout_support_bot.sh")

    assert 'tar -czf "${release_archive}" -C "${release_stage}" .' in deploy
    package_start = deploy.index('qpi_phase_start "package"')
    package_end = deploy.index("qpi_phase_end", package_start)
    package_block = deploy[package_start:package_end]

    assert 'write_env_file "${release_env_file}"' in package_block
    assert 'write_env_file "${release_stage}/.env"' not in package_block
    assert '"${release_env_file}" \\' in deploy
    assert '$(basename "${release_env_file}")' in deploy
    expected_cleanup = (
        'rm -f /tmp/remote_rollout_support_bot.sh /tmp/$(basename "${release_archive:-}") '
        '/tmp/$(basename "${release_env_file:-}")'
    )
    assert expected_cleanup in deploy

    assert 'sudo install -m 0600 -o ubuntu -g ubuntu "${env_path}" "${release_dir}/.env"' in remote_rollout
    assert "sudo chown -R ubuntu:ubuntu" not in remote_rollout
