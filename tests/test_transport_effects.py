from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from libs.config.settings import BotApiSettings
from services.bot_api.callback_data import build_callback
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from services.bot_api.transport_effects import (
    AnswerCallback,
    ButtonSpec,
    ClearPrompt,
    DeleteSourceMessage,
    FlowResult,
    LogEvent,
    ReplaceText,
    ReplyPhoto,
    ReplyRoleMenuText,
    ReplyText,
    SetPrompt,
    SetUserData,
)
from tests.e2e_harness import FakeBot, FakeCallbackQuery, FakeChat, FakeContext, FakeMessage, FakeTransport


def _build_runtime() -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [1],
            "DISPLAY_RUB_PER_USDT": "100",
        }
    )
    return TelegramWebhookRuntime(settings=settings)


def test_button_spec_describes_callback_or_url_button() -> None:
    callback = ButtonSpec(text="↩️ Назад", flow="seller", action="listings", entity_id="2")
    url = ButtonSpec(text="🆘 Поддержка", url="https://t.me/support_bot")

    assert callback.flow == "seller"
    assert callback.action == "listings"
    assert callback.entity_id == "2"
    assert url.url == "https://t.me/support_bot"

    with pytest.raises(ValueError):
        ButtonSpec(text="Пусто")
    with pytest.raises(ValueError):
        ButtonSpec(text="Два типа", flow="seller", action="listings", url="https://example.com")


def test_seller_listing_creation_start_prompt_uses_shared_transport_effects() -> None:
    flow = SellerListingCreationFlow(
        seller_service=SimpleNamespace(),
        seller_workflow=SimpleNamespace(),
        display_rub_per_usdt=Decimal("100"),
    )

    result = flow.start_prompt(seller_user_id=7, shop_id=11, shop_title="Тушенка")

    assert result.__class__.__module__ == "services.bot_api.transport_effects"
    assert [effect.__class__.__module__ for effect in result.effects] == [
        "services.bot_api.transport_effects",
        "services.bot_api.transport_effects",
    ]
    assert isinstance(result.effects[0], SetPrompt)


def test_runtime_caches_stateless_flow_factories_and_refreshes_buyer_marketplace_rate() -> None:
    runtime = _build_runtime()

    assert runtime._seller_withdrawal_creation_flow() is runtime._seller_withdrawal_creation_flow()
    assert runtime._buyer_withdrawal_creation_flow() is runtime._buyer_withdrawal_creation_flow()
    assert runtime._admin_exceptions_flow() is runtime._admin_exceptions_flow()

    buyer_flow = runtime._buyer_marketplace_flow()
    assert runtime._buyer_marketplace_flow() is buyer_flow

    runtime._display_rub_per_usdt = Decimal("101")
    refreshed_buyer_flow = runtime._buyer_marketplace_flow()

    assert refreshed_buyer_flow is not buyer_flow
    assert refreshed_buyer_flow._config.display_rub_per_usdt == Decimal("101")


@pytest.mark.asyncio
async def test_runtime_applies_shared_transport_effects_to_telegram_adapter() -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    transport = FakeTransport()
    chat = FakeChat(transport=transport, chat_id=100)
    message = FakeMessage(transport=transport, chat=chat)
    context = FakeContext(bot=FakeBot(transport=transport))

    await runtime._apply_transport_effects(
        context=context,
        query_message=None,
        message=message,
        default_role="seller",
        result=FlowResult(
            effects=(
                SetPrompt(prompt_type="seller_listing_create", data={"shop_id": 11}),
                SetUserData(key="last_buyer_shop_slug", value="shop_tushenka"),
                ReplyPhoto(photo_url="https://example.com/photo.webp"),
                ReplyText(
                    text="Экран",
                    buttons=(
                        (
                            ButtonSpec(text="📦 К объявлениям", flow="seller", action="listings", entity_id="3"),
                            ButtonSpec(text="🆘 Поддержка", url="https://t.me/support_bot"),
                        ),
                    ),
                ),
                LogEvent(event_name="transport_effect_test", fields={"shop_id": 11}),
            )
        ),
    )

    assert context.user_data["prompt_state"] == {
        "role": "seller",
        "type": "seller_listing_create",
        "sensitive": False,
        "shop_id": 11,
    }
    assert context.user_data["last_buyer_shop_slug"] == "shop_tushenka"
    assert [event.kind for event in transport.events] == ["reply_photo", "reply"]
    reply_markup = transport.events[-1].reply_markup
    first_button, second_button = reply_markup.inline_keyboard[0]
    assert first_button.callback_data == build_callback(flow="seller", action="listings", entity_id="3")
    assert second_button.url == "https://t.me/support_bot"
    runtime._logger.info.assert_called_once_with("transport_effect_test", shop_id=11)


@pytest.mark.asyncio
async def test_runtime_applies_prompt_clear_callback_feedback_and_source_delete_effects() -> None:
    runtime = _build_runtime()
    transport = FakeTransport()
    chat = FakeChat(transport=transport, chat_id=100)
    message = FakeMessage(transport=transport, chat=chat)
    callback_query = FakeCallbackQuery(
        transport=transport,
        callback_data=build_callback(flow="seller", action="listings"),
        from_user=SimpleNamespace(id=100, username="seller"),
        message=message,
    )
    context = FakeContext(bot=FakeBot(transport=transport), user_data={"prompt_state": {"type": "old"}})

    await runtime._apply_transport_effects(
        context=context,
        query_message=message,
        message=None,
        default_role="seller",
        callback_query=callback_query,
        result=FlowResult(
            effects=(
                ClearPrompt(),
                AnswerCallback(text="Готово", show_alert=True),
                DeleteSourceMessage(),
            )
        ),
    )

    assert "prompt_state" not in context.user_data
    assert [event.kind for event in transport.events] == ["callback_answer", "delete"]
    assert transport.events[0].text == "Готово"
    assert transport.events[0].show_alert is True


@pytest.mark.asyncio
async def test_runtime_warns_when_user_visible_transport_effect_has_no_target() -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    context = FakeContext(bot=FakeBot(transport=FakeTransport()))

    await runtime._apply_transport_effects(
        context=context,
        query_message=None,
        message=None,
        default_role="admin",
        result=FlowResult(
            effects=(
                AnswerCallback(text="Готово"),
                ReplyPhoto(photo_url="https://example.com/photo.webp"),
                ReplyText(text="Экран"),
                ReplyRoleMenuText(text="Меню", role="admin"),
                ReplaceText(text="Новый экран"),
            )
        ),
    )

    assert runtime._logger.warning.call_count == 5
    warning_calls = runtime._logger.warning.call_args_list
    assert [call.args[0] for call in warning_calls] == ["telegram_transport_effect_dropped"] * 5
    assert [call.kwargs["effect_type"] for call in warning_calls] == [
        "AnswerCallback",
        "ReplyPhoto",
        "ReplyText",
        "ReplyRoleMenuText",
        "ReplaceText",
    ]
    assert [call.kwargs["reason"] for call in warning_calls] == [
        "missing_callback_query",
        "missing_message",
        "missing_message",
        "missing_message",
        "missing_message",
    ]
