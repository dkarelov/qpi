import pytest


def test_private_user_texts_are_russian_even_for_english_telegram_locale() -> None:
    from app.bot.utils.texts import TextMessage

    text = TextMessage("en")

    assert "Здравствуйте" in text.get("main_menu")
    assert "Сообщение отправлено" in text.get("message_sent")
    assert "Message sent" not in text.get("message_sent")


def test_russian_is_the_only_supported_end_user_language() -> None:
    from app.bot.utils.texts import SUPPORTED_LANGUAGES

    assert SUPPORTED_LANGUAGES == {"ru": "🇷🇺 Русский"}


@pytest.mark.asyncio
async def test_user_success_ack_is_sent_only_after_topic_delivery() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            self.events.append(f"create:{title}")
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            self.events.append(f"topic:{text}")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            self.events.append(f"private:{text}")

        async def send_user_ack(self, *, telegram_id: int, text: str, ttl_seconds: int) -> None:
            assert ttl_seconds == 5
            self.events.append(f"ack:{text}")

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )

    await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Нужна помощь")

    assert telegram.events == [
        "create:Ivan",
        "topic:Нужна помощь",
        "ack:Сообщение отправлено в поддержку. Ответим здесь.",
    ]


@pytest.mark.asyncio
async def test_start_command_records_payload_and_opens_main_menu_without_language_selector() -> None:
    from aiogram.filters import CommandObject

    from app.bot.handlers.private.command import start_handler
    from app.bot.utils.redis.models import UserData
    from app.bot.utils.texts import TextMessage
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    class FakeState:
        def __init__(self) -> None:
            self.state: object = "old"

        async def set_state(self, state: object) -> None:
            self.state = state

    class FakeManager:
        def __init__(self) -> None:
            self.config = Config(
                bot=BotConfig(TOKEN="123:token", DEV_IDS=[111], GROUP_ID=-1004355595623),
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
            self.text_message = TextMessage("en")
            self.user = type("User", (), {"full_name": "Карина"})()
            self.state = FakeState()
            self.sent: list[tuple[str, object | None]] = []
            self.deleted = 0

        async def send_message(self, text: str, reply_markup: object | None = None) -> None:
            self.sent.append((text, reply_markup))

        async def delete_message(self, _message: object) -> None:
            self.deleted += 1

    class FakeRedis:
        def __init__(self) -> None:
            self.updated: list[UserData] = []

        async def update_user(self, _id: int, data: UserData) -> None:
            self.updated.append(data)

    user_data = UserData(
        message_thread_id=None,
        message_silent_mode=False,
        id=1001,
        full_name="Карина",
        username="-",
    )
    manager = FakeManager()

    await start_handler(
        message=type("Message", (), {"bot": object()})(),  # type: ignore[arg-type]
        command=CommandObject(command="start", args="seller_listing_L21_S11"),
        manager=manager,  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        user_data=user_data,
    )

    assert user_data.language_code == "ru"
    assert user_data.support_refs == ("L21", "S11")
    assert manager.sent == [("<b>Здравствуйте!</b> Напишите ваш вопрос — ответим в ближайшее время.", None)]
    assert manager.deleted == 1


@pytest.mark.asyncio
async def test_user_delivery_failure_is_persistent_russian_and_hides_exception_details() -> None:
    from app.bot.support_topics import InMemorySupportTopicStore, SupportTopicService, TelegramAccount

    class FakeTelegram:
        def __init__(self) -> None:
            self.failures: list[tuple[str, bool]] = []

        async def create_topic(self, *, group_id: int, title: str) -> int:
            return 701

        async def send_topic_text(self, *, group_id: int, thread_id: int, text: str) -> None:
            raise RuntimeError("database password leaked in internal stack")

        async def send_private_text(self, *, telegram_id: int, text: str) -> None:
            raise AssertionError("staff reply is not part of this test")

        async def send_user_failure(self, *, telegram_id: int, text: str, persistent: bool) -> None:
            self.failures.append((text, persistent))

    telegram = FakeTelegram()
    service = SupportTopicService(
        store=InMemorySupportTopicStore(),
        telegram=telegram,
        group_id=-1001234567890,
    )

    result = await service.forward_user_text(TelegramAccount(id=1001, full_name="Ivan"), "Нужна помощь")

    assert result is None
    assert telegram.failures == [
        ("Не удалось отправить сообщение в поддержку. Пожалуйста, попробуйте ещё раз через пару минут.", True)
    ]
    assert "password" not in telegram.failures[0][0]
    assert "stack" not in telegram.failures[0][0]


@pytest.mark.asyncio
async def test_private_message_is_not_forwarded_to_general_group_when_topic_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.bot.handlers.private import message as private_message
    from app.bot.support_topics import USER_DELIVERY_FAILURE
    from app.bot.utils.redis.models import UserData
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(private_message.asyncio, "sleep", no_sleep)

    class FakeBot:
        def __init__(self) -> None:
            self.admin_messages: list[tuple[int, str]] = []

        async def create_forum_topic(self, **_kwargs: object) -> object:
            raise RuntimeError("not enough rights to create a topic")

        async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
            self.admin_messages.append((chat_id, text))

    class FakeReply:
        async def delete(self) -> None:
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.text = "Нужна помощь"
            self.caption = None
            self.forwards: list[tuple[int, int | None]] = []
            self.replies: list[str] = []

        async def forward(self, *, chat_id: int, message_thread_id: int | None = None) -> None:
            self.forwards.append((chat_id, message_thread_id))

        async def reply(self, text: str) -> FakeReply:
            self.replies.append(text)
            return FakeReply()

    class FakeRedis:
        def __init__(self) -> None:
            self.conversation: list[tuple[int, str, str]] = []
            self.updated: list[UserData] = []

        async def append_conversation(self, user_id: int, role: str, text: str) -> None:
            self.conversation.append((user_id, role, text))

        async def update_user(self, _id: int, data: UserData) -> None:
            self.updated.append(data)

    class FakeManager:
        config = Config(
            bot=BotConfig(TOKEN="123:token", DEV_IDS=[111], GROUP_ID=-1004355595623),
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

    message = FakeMessage()
    redis = FakeRedis()
    user_data = UserData(
        message_thread_id=None,
        message_silent_mode=False,
        id=1001,
        full_name="Ivan",
        username="ivan",
    )

    await private_message.handle_incoming_message(
        message=message,  # type: ignore[arg-type]
        manager=FakeManager(),  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_data=user_data,
    )

    assert message.forwards == []
    assert message.replies == [USER_DELIVERY_FAILURE]
    assert message.bot.admin_messages == [(111, "not enough rights to create a topic")]


@pytest.mark.asyncio
async def test_reopened_topic_does_not_pin_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.bot.handlers.private import message as private_message
    from app.bot.utils.redis.models import UserData
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(private_message.asyncio, "sleep", no_sleep)

    class FakeBot:
        def __init__(self) -> None:
            self.reopened_threads: list[tuple[int, int]] = []
            self.metadata_messages: list[str] = []

        async def reopen_forum_topic(self, *, chat_id: int, message_thread_id: int) -> None:
            self.reopened_threads.append((chat_id, message_thread_id))

        async def send_message(
            self,
            chat_id: int,
            text: str,
            *,
            message_thread_id: int | None = None,
            **_kwargs: object,
        ) -> None:
            self.metadata_messages.append(text)

    class FakeReply:
        async def delete(self) -> None:
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.text = "Переоткрываю обращение."
            self.caption = None
            self.forwards: list[tuple[int, int | None]] = []
            self.replies: list[str] = []

        async def forward(self, *, chat_id: int, message_thread_id: int | None = None) -> None:
            self.forwards.append((chat_id, message_thread_id))

        async def reply(self, text: str) -> FakeReply:
            self.replies.append(text)
            return FakeReply()

    class FakeRedis:
        def __init__(self) -> None:
            self.conversation: list[tuple[int, str, str]] = []
            self.updated: list[UserData] = []

        async def append_conversation(self, user_id: int, role: str, text: str) -> None:
            self.conversation.append((user_id, role, text))

        async def update_user(self, _id: int, data: UserData) -> None:
            self.updated.append(data)

    class FakeManager:
        config = Config(
            bot=BotConfig(TOKEN="123:token", DEV_IDS=[111], GROUP_ID=-1004355595623),
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

    message = FakeMessage()
    redis = FakeRedis()
    user_data = UserData(
        message_thread_id=701,
        message_silent_mode=False,
        id=1001,
        full_name="Карина",
        username="-",
        status="closed",
        support_role="seller",
        support_topic="listing",
        support_refs=("L21", "S11"),
    )

    await private_message.handle_incoming_message(
        message=message,  # type: ignore[arg-type]
        manager=FakeManager(),  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_data=user_data,
    )

    assert message.bot.reopened_threads == [(-1004355595623, 701)]
    assert message.forwards == [(-1004355595623, 701)]
    assert any(update.status == "open" for update in redis.updated)
    assert message.bot.metadata_messages == []


@pytest.mark.asyncio
async def test_failed_reopen_keeps_live_topic_closed_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.exceptions import TelegramBadRequest

    from app.bot.handlers.private import message as private_message
    from app.bot.support_topics import USER_DELIVERY_FAILURE
    from app.bot.utils.redis.models import UserData
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(private_message.asyncio, "sleep", no_sleep)

    class FakeBot:
        async def reopen_forum_topic(self, *, chat_id: int, message_thread_id: int) -> None:
            raise TelegramBadRequest(method=object(), message="Bad Request: topic is closed")

        async def send_message(self, *_args: object, **_kwargs: object) -> None:
            return None

    class FakeReply:
        async def delete(self) -> None:
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.text = "Переоткрываю обращение."
            self.caption = None
            self.forwards: list[tuple[int, int | None]] = []
            self.replies: list[str] = []

        async def forward(self, *, chat_id: int, message_thread_id: int | None = None) -> None:
            self.forwards.append((chat_id, message_thread_id))

        async def reply(self, text: str) -> FakeReply:
            self.replies.append(text)
            return FakeReply()

    class FakeRedis:
        def __init__(self) -> None:
            self.conversation: list[tuple[int, str, str]] = []
            self.updated: list[UserData] = []

        async def append_conversation(self, user_id: int, role: str, text: str) -> None:
            self.conversation.append((user_id, role, text))

        async def update_user(self, _id: int, data: UserData) -> None:
            self.updated.append(data)

    class FakeManager:
        config = Config(
            bot=BotConfig(TOKEN="123:token", DEV_IDS=[111], GROUP_ID=-1004355595623),
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

    message = FakeMessage()
    redis = FakeRedis()
    user_data = UserData(
        message_thread_id=701,
        message_silent_mode=False,
        id=1001,
        full_name="Карина",
        username="-",
        status="closed",
    )

    await private_message.handle_incoming_message(
        message=message,  # type: ignore[arg-type]
        manager=FakeManager(),  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_data=user_data,
    )

    assert message.forwards == []
    assert message.replies == [USER_DELIVERY_FAILURE]
    assert user_data.status == "closed"
    assert not any(update.status == "open" for update in redis.updated)


@pytest.mark.asyncio
async def test_deleted_topic_recovery_creates_replacement_with_context_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.exceptions import TelegramBadRequest

    from app.bot.handlers.private import message as private_message
    from app.bot.utils.redis.models import UserData
    from app.config import AIConfig, BotConfig, Config, DatabaseConfig, PolicyConfig, RedisConfig, TelegramConfig

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(private_message.asyncio, "sleep", no_sleep)

    class FakeForumTopic:
        message_thread_id = 702

    class FakeBot:
        def __init__(self) -> None:
            self.created_topic_names: list[str] = []

        async def create_forum_topic(self, *, name: str, **_kwargs: object) -> FakeForumTopic:
            self.created_topic_names.append(name)
            return FakeForumTopic()

    class FakeReply:
        async def delete(self) -> None:
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.text = "Снова пишу после удаления темы."
            self.caption = None
            self.forwards: list[tuple[int, int | None]] = []
            self.replies: list[str] = []

        async def forward(self, *, chat_id: int, message_thread_id: int | None = None) -> None:
            if message_thread_id == 701:
                raise TelegramBadRequest(method=object(), message="Bad Request: message thread not found")
            self.forwards.append((chat_id, message_thread_id))

        async def reply(self, text: str) -> FakeReply:
            self.replies.append(text)
            return FakeReply()

    class FakeRedis:
        def __init__(self) -> None:
            self.conversation: list[tuple[int, str, str]] = []
            self.updated: list[UserData] = []

        async def append_conversation(self, user_id: int, role: str, text: str) -> None:
            self.conversation.append((user_id, role, text))

        async def update_user(self, _id: int, data: UserData) -> None:
            self.updated.append(data)

    class FakeManager:
        config = Config(
            bot=BotConfig(TOKEN="123:token", DEV_IDS=[111], GROUP_ID=-1004355595623),
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

    message = FakeMessage()
    redis = FakeRedis()
    user_data = UserData(
        message_thread_id=701,
        message_silent_mode=False,
        id=1001,
        full_name="Карина",
        username="-",
        status="open",
        support_role="seller",
        support_topic="listing",
        support_refs=("L21", "S11"),
    )

    await private_message.handle_incoming_message(
        message=message,  # type: ignore[arg-type]
        manager=FakeManager(),  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        user_data=user_data,
    )

    assert message.bot.created_topic_names == ["Карина · Seller listing · L21 S11"]
    assert message.forwards == [(-1004355595623, 702)]
    assert message.replies == ["Сообщение отправлено в поддержку. Ответим здесь."]
    assert user_data.message_thread_id == 702
    assert any(update.message_thread_id == 702 for update in redis.updated)
