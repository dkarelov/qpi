from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import services.bot_api.telegram_runtime as telegram_runtime
from libs.config.settings import BotApiSettings
from services.bot_api.callback_data import build_callback
from services.bot_api.telegram_runtime import TelegramWebhookRuntime


def _settings(**overrides: object) -> BotApiSettings:
    data: dict[str, object] = {
        "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
        "TOKEN_CIPHER_KEY": "test-key",
        "TELEGRAM_BOT_TOKEN": "123:test",
    }
    data.update(overrides)
    return BotApiSettings.model_validate(data)


class _FakeApplication:
    def __init__(self) -> None:
        self.polling_kwargs: dict[str, Any] | None = None
        self.webhook_kwargs: dict[str, Any] | None = None

    def run_polling(self, **kwargs: Any) -> None:
        self.polling_kwargs = kwargs

    def run_webhook(self, **kwargs: Any) -> None:
        self.webhook_kwargs = kwargs


class _FakeHealthServer:
    def __init__(self, **kwargs: Any) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_runtime_polling_mode_calls_run_polling_and_does_not_require_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _FakeApplication()
    health = _FakeHealthServer()
    runtime = TelegramWebhookRuntime(settings=_settings(TELEGRAM_UPDATE_MODE="polling", WEBHOOK_BASE_URL=""))
    monkeypatch.setattr(runtime, "_build_application", lambda: app)
    monkeypatch.setattr(
        runtime,
        "_build_webhook_url",
        lambda: (_ for _ in ()).throw(AssertionError("webhook URL used")),
    )
    monkeypatch.setattr(telegram_runtime, "_BotHealthServer", lambda **kwargs: health)

    runtime.run()

    assert health.started is True
    assert health.stopped is True
    assert app.polling_kwargs == {
        "allowed_updates": telegram_runtime.Update.ALL_TYPES,
        "drop_pending_updates": False,
    }
    assert app.webhook_kwargs is None


def test_runtime_webhook_mode_keeps_explicit_webhook_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _FakeApplication()
    runtime = TelegramWebhookRuntime(
        settings=_settings(
            TELEGRAM_UPDATE_MODE="webhook",
            WEBHOOK_BASE_URL="https://bot.example",
            WEBHOOK_PATH="telegram/webhook",
        )
    )
    monkeypatch.setattr(runtime, "_build_application", lambda: app)
    monkeypatch.setattr(telegram_runtime, "_BotHealthServer", lambda **kwargs: _FakeHealthServer())

    runtime.run()

    assert app.polling_kwargs is None
    assert app.webhook_kwargs["webhook_url"] == "https://bot.example/telegram/webhook"
    assert app.webhook_kwargs["drop_pending_updates"] is False
    assert app.webhook_kwargs["allowed_updates"] == telegram_runtime.Update.ALL_TYPES


@pytest.mark.asyncio
async def test_callback_received_is_logged_before_answer_callback_query() -> None:
    events: list[tuple[str, str | None]] = []

    class Logger:
        def info(self, event_name: str, **fields: object) -> None:
            events.append(("log", event_name))

        def warning(self, event_name: str, **fields: object) -> None:
            events.append(("warning", event_name))

        def exception(self, event_name: str, **fields: object) -> None:
            events.append(("exception", event_name))

    class Message:
        async def edit_reply_markup(self, reply_markup: object | None = None) -> None:
            events.append(("edit_reply_markup", None))

        async def reply_text(self, text: str, **kwargs: object) -> None:
            events.append(("reply_text", text))

    class CallbackQuery:
        data = build_callback(flow="unknown", action="noop")
        id = "cbq-1"
        from_user = SimpleNamespace(id=20001, username="buyer")
        message = Message()

        async def answer(self, text: str | None = None, *, show_alert: bool | None = None) -> None:
            events.append(("answer", text))

    runtime = TelegramWebhookRuntime(settings=_settings(), logger=Logger())  # type: ignore[arg-type]
    update = SimpleNamespace(update_id=101, message=None, callback_query=CallbackQuery())
    context = SimpleNamespace(user_data={})

    await runtime._handle_callback(update, context)

    callback_log_index = events.index(("log", "telegram_callback_received"))
    answer_index = next(index for index, event in enumerate(events) if event[0] == "answer")
    assert callback_log_index < answer_index
