import pytest


def test_load_config_uses_qpi_env_and_first_telegram_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_BOT_TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("SUPPORT_BOT_GROUP_ID", "-1001234567890")
    monkeypatch.setenv("SUPPORT_BOT_OWNER_ID", "111")
    monkeypatch.setenv("SUPPORT_BOT_DEV_IDS", "222,333")
    monkeypatch.setenv("DATABASE_URL", "postgresql://support:secret@db.local:5432/qpi")
    monkeypatch.setenv("SUPPORT_BOT_DB_SCHEMA", "support_bot")
    monkeypatch.setenv("REDIS_HOST", "support-bot-redis")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("REDIS_DB", "7")
    monkeypatch.setenv("REDIS_PASSWORD", "")
    monkeypatch.setenv(
        "TELEGRAM_API_PROXY_URLS",
        "http://proxy-a.example:8080, http://proxy-b.example:8080",
    )

    from app.config import load_config

    config = load_config()

    assert config.bot.TOKEN == "123:token"
    assert config.bot.GROUP_ID == -1001234567890
    assert config.bot.DEV_IDS == [222, 333]
    assert config.bot.DEV_ID == 222
    assert config.db.URL == "postgresql://support:secret@db.local:5432/qpi"
    assert config.db.SCHEMA == "support_bot"
    assert config.telegram.PROXY_URL == "http://proxy-a.example:8080"


def test_user_data_created_at_uses_per_instance_default_factory() -> None:
    from dataclasses import MISSING

    from app.bot.storage import UserData

    created_at_field = UserData.__dataclass_fields__["created_at"]

    assert created_at_field.default is MISSING
    assert created_at_field.default_factory is not MISSING


@pytest.mark.asyncio
async def test_create_bot_supports_configured_proxy() -> None:
    from app.bot.telegram_client import create_bot
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    bot = create_bot(
        Config(
            bot=BotConfig(TOKEN="123456:abcdefghijklmnopqrstuvwxyz", DEV_IDS=[111], GROUP_ID=-1001234567890),
            redis=RedisConfig(HOST="redis", PORT=6379, DB=7),
            db=DatabaseConfig(URL="postgresql://support:secret@db.local:5432/qpi"),
            telegram=TelegramConfig(PROXY_URL="http://proxy.example:8080"),
            policy=PolicyConfig(ENABLED=False, PATH="config/policy.yaml"),
            ai=AIConfig(
                PROVIDER="none",
                BASE_URL="https://openrouter.ai/api/v1",
                API_KEY="",
                MODEL="openai/gpt-5.4-nano",
                SYSTEM_PROMPT_PATH="config/system_prompt.txt",
                TIMEOUT_S=8,
            ),
        )
    )
    await bot.session.close()


@pytest.mark.asyncio
async def test_create_schema_uses_support_bot_schema() -> None:
    from app.bot.storage import create_schema

    statements: list[str] = []

    class FakeConnection:
        async def execute(self, statement: str, *args: object) -> None:
            assert args == ()
            statements.append(statement)

    class FakeAcquire:
        async def __aenter__(self) -> FakeConnection:
            return FakeConnection()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakePool:
        def acquire(self) -> FakeAcquire:
            return FakeAcquire()

    await create_schema(FakePool(), schema="support_bot")

    rendered = "\n".join(statements)
    assert "CREATE SCHEMA IF NOT EXISTS support_bot" in rendered
    assert "support_bot.users" in rendered
    assert "support_bot.conversations" in rendered
    assert "support_role TEXT" in rendered
    assert "support_topic TEXT" in rendered
    assert "support_refs TEXT[]" in rendered
    assert "public.users" not in rendered


@pytest.mark.asyncio
async def test_text_round_trip_creates_support_topic_and_reuses_it() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.next_thread_id = 700
            self.created_topics: list[str] = []
            self.topic_messages: list[tuple[int, int, str]] = []
            self.private_messages: list[tuple[int, str]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            assert group_id == -1001234567890
            self.created_topics.append(title)
            self.next_thread_id += 1
            return self.next_thread_id

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.topic_messages.append((group_id, thread_id, text))

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            self.private_messages.append((telegram_id, text))

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            return None

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )
    account = TelegramAccount(id=1001, full_name="Ivan Buyer", username="ivan")

    topic = await service.forward_user_text(account, "Первый вопрос")
    same_topic = await service.forward_user_text(account, "Второй вопрос")
    assert topic is not None
    assert same_topic is not None
    await service.forward_staff_text(thread_id=topic.thread_id, text="Ответ поддержки")

    assert same_topic.thread_id == topic.thread_id
    assert telegram.created_topics == ["Ivan Buyer"]
    assert telegram.topic_messages == [
        (-1001234567890, topic.thread_id, "Первый вопрос"),
        (-1001234567890, topic.thread_id, "Второй вопрос"),
    ]
    assert telegram.private_messages == [(1001, "Ответ поддержки")]
