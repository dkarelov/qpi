from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_support_topic_glossary_and_adr_are_present() -> None:
    context = _read("CONTEXT.md")
    adr = _read("docs", "adr", "0002-support-topic-replacement.md")

    assert "**Support Topic**" in context
    assert "one Telegram forum topic per **Telegram Account**" in context

    assert "forum-topic model" in adr
    assert "immediate no-migration cutover" in adr
    assert "existing PostgreSQL cluster" in adr
    assert "ephemeral Redis" in adr
    assert "manual upstream updates" in adr
    assert "old Mongo data" in adr


def test_support_bot_docs_do_not_preserve_old_node_mongo_runbooks() -> None:
    docs = {
        "AGENTS.md": _read("AGENTS.md"),
        "README.md": _read("README.md"),
        "docs/dev_workflow.md": _read("docs", "dev_workflow.md"),
        "apps/support-bot/README.local.md": _read("apps", "support-bot", "README.local.md"),
    }

    forbidden_fragments = [
        "Node 24",
        "npm ci",
        "npm run",
        "Local Mongo for support-bot",
        "mongodb mongosh",
        "resendOrphanTickets",
        "staffchat_id",
        "auto_close_tickets",
    ]

    for path, text in docs.items():
        for fragment in forbidden_fragments:
            assert fragment not in text, f"{path} still contains stale support-bot fragment: {fragment}"


def test_support_bot_runbooks_name_current_runtime_inputs_and_out_of_scope_items() -> None:
    readme = _read("apps", "support-bot", "README.local.md")
    agents = _read("AGENTS.md")
    combined = f"{readme}\n{agents}"

    for required in [
        "SUPPORT_BOT_GROUP_ID",
        "SUPPORT_BOT_DATABASE_URL",
        "SUPPORT_BOT_DB_SCHEMA",
        "SUPPORT_BOT_REDIS_DB",
        "TELEGRAM_API_PROXY_URLS",
        "Redis PING",
        "PostgreSQL schema",
        "Support Topic",
        "old Mongo data",
        "`/open`",
        "orphan-ticket recovery",
        "old ticket ids",
        "private staff group",
        "DefaultPerson/telegram-support-bot",
        "b74e7b73107ea1f59cc05b878a488470fc84bd6b",
    ]:
        assert required in combined
