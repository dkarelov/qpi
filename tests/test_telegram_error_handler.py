from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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

