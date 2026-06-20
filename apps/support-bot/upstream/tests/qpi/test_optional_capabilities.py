from pathlib import Path


def test_policy_and_ai_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_BOT_TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("SUPPORT_BOT_GROUP_ID", "-1001234567890")
    monkeypatch.setenv("SUPPORT_BOT_OWNER_ID", "111")
    monkeypatch.setenv("DATABASE_URL", "postgresql://support:secret@db.local:5432/qpi")

    from app.config import load_config

    config = load_config()

    assert config.policy.ENABLED is False
    assert config.ai.PROVIDER == "none"
    assert config.ai.API_KEY == ""


def test_production_dependencies_include_llm_provider_without_enabling_ai() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '"openai' in pyproject
    assert 'AI_PROVIDER", "none"' not in pyproject


def test_newsletter_registration_is_idempotent_per_telegram_account() -> None:
    from app.bot.newsletter import InMemoryNewsletterRegistry, NewsletterService
    from app.bot.support_topics import TelegramAccount

    registry = InMemoryNewsletterRegistry()
    service = NewsletterService(registry)
    account = TelegramAccount(id=1001, full_name="Ivan", username="ivan")

    service.register(account)
    service.register(account)

    assert service.subscriber_ids() == [1001]
