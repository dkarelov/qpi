from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.bot_api.telegram_runtime as telegram_runtime_module
from libs.config.settings import BotApiSettings
from libs.domain.errors import InvalidStateError
from services.bot_api.telegram_runtime import TelegramWebhookRuntime


def _build_runtime() -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "phase10-test-key",
        }
    )
    return TelegramWebhookRuntime(settings=settings)


class _FakeApplicationBuilder:
    def __init__(self) -> None:
        self.token_value: str | None = None
        self.proxy_value: str | None = None

    def token(self, value: str) -> _FakeApplicationBuilder:
        self.token_value = value
        return self

    def post_init(self, _callback: object) -> _FakeApplicationBuilder:
        return self

    def post_shutdown(self, _callback: object) -> _FakeApplicationBuilder:
        return self

    def proxy(self, value: str) -> _FakeApplicationBuilder:
        self.proxy_value = value
        return self

    def build(self) -> _FakeApplication:
        return _FakeApplication()


class _FakeApplication:
    def add_handler(self, _handler: object) -> None:
        return None

    def add_error_handler(self, _handler: object) -> None:
        return None


@pytest.mark.asyncio
async def test_handle_error_notifies_user_for_domain_error() -> None:
    runtime = _build_runtime()
    reply_text = AsyncMock()
    update = SimpleNamespace(update_id=1, effective_message=SimpleNamespace(reply_text=reply_text))
    context = SimpleNamespace(error=InvalidStateError("boom"))

    await runtime._handle_error(update, context)

    reply_text.assert_awaited_once()
    assert "Попробуйте еще раз" in reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_handle_error_notifies_user_for_unexpected_error() -> None:
    runtime = _build_runtime()
    reply_text = AsyncMock()
    update = SimpleNamespace(update_id=2, effective_message=SimpleNamespace(reply_text=reply_text))
    context = SimpleNamespace(error=RuntimeError("boom"))

    await runtime._handle_error(update, context)

    reply_text.assert_awaited_once()
    assert "Произошла ошибка" in reply_text.await_args.args[0]


def test_build_application_passes_configured_proxy_to_telegram_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _FakeApplicationBuilder()
    monkeypatch.setattr(telegram_runtime_module.Application, "builder", lambda: builder)
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TELEGRAM_BOT_TOKEN": "123:test-token",
            "TELEGRAM_API_PROXY_URL": "http://proxy.example:8000",
            "TOKEN_CIPHER_KEY": "phase10-test-key",
        }
    )
    runtime = TelegramWebhookRuntime(settings=settings)

    runtime._build_application()

    assert builder.token_value == "123:test-token"
    assert builder.proxy_value == "http://proxy.example:8000"
