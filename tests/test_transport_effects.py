from __future__ import annotations

import io
import urllib.request
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from telegram import InputFile

import services.bot_api.telegram_runtime as telegram_runtime_module
from libs.config.settings import BotApiSettings
from services.bot_api.callback_data import build_callback
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.telegram_runtime import TelegramWebhookRuntime, _DownloadedPhoto, _PhotoDownloadError
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


class FailingPhotoMessage(FakeMessage):
    def __init__(self, *, failures: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._remaining_photo_failures = failures

    async def reply_photo(self, photo, **kwargs) -> None:
        if self._remaining_photo_failures > 0:
            self._remaining_photo_failures -= 1
            raise RuntimeError("simulated photo failure")
        await super().reply_photo(photo, **kwargs)


class FakePhotoResponse:
    def __init__(
        self,
        *,
        data: bytes,
        content_type: str | None,
        final_url: str,
        status: int = 200,
        content_length: int | None = None,
    ) -> None:
        self._data = data
        self._final_url = final_url
        self.status = status
        self.headers = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> FakePhotoResponse:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._data[:size]

    def geturl(self) -> str:
        return self._final_url


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


def test_wb_photo_url_matching_uses_hostname_and_rejects_spoofed_hosts() -> None:
    assert telegram_runtime_module._is_wb_photo_url("https://wbbasket.ru/item.webp")
    assert telegram_runtime_module._is_wb_photo_url("https://basket-41.wbbasket.ru:443/item.webp")
    assert telegram_runtime_module._is_wb_photo_url("https://images.wbcontent.net/item.webp")
    assert telegram_runtime_module._is_wb_photo_url("https://basket-41.wb.ru/item.webp")

    assert not telegram_runtime_module._is_wb_photo_url("https://basket-41.wbbasket.ru.evil.example/item.webp")
    assert not telegram_runtime_module._is_wb_photo_url("https://basket-41.wbbasket.ru@169.254.169.254/item.webp")
    assert not telegram_runtime_module._is_wb_photo_url("https://example.com/item.webp")


def test_download_photo_rejects_untrusted_url_without_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_opener = SimpleNamespace(open=Mock())
    monkeypatch.setattr(telegram_runtime_module, "_PHOTO_URL_OPENER", fake_opener)

    with pytest.raises(_PhotoDownloadError, match="trusted WB photo host"):
        telegram_runtime_module._download_photo_from_url("https://example.com/item.jpg")

    fake_opener.open.assert_not_called()


def test_download_photo_rejects_untrusted_final_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_opener = SimpleNamespace(
        open=Mock(
            return_value=FakePhotoResponse(
                data=b"image-bytes",
                content_type="image/webp",
                final_url="http://169.254.169.254/latest/meta-data",
            )
        )
    )
    monkeypatch.setattr(telegram_runtime_module, "_PHOTO_URL_OPENER", fake_opener)

    with pytest.raises(_PhotoDownloadError, match="final photo URL"):
        telegram_runtime_module._download_photo_from_url("https://basket-41.wbbasket.ru/item.webp")


def test_trusted_photo_redirect_handler_rejects_untrusted_redirect() -> None:
    handler = telegram_runtime_module._TrustedPhotoRedirectHandler()
    request = urllib.request.Request("https://basket-41.wbbasket.ru/item.webp")

    with pytest.raises(_PhotoDownloadError, match="redirect target"):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data",
        )


def test_download_photo_allows_binary_content_type_from_trusted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_opener = SimpleNamespace(
        open=Mock(
            return_value=FakePhotoResponse(
                data=b"image-bytes",
                content_type="application/octet-stream",
                final_url="https://basket-41.wbbasket.ru/item.webp",
            )
        )
    )
    monkeypatch.setattr(telegram_runtime_module, "_PHOTO_URL_OPENER", fake_opener)

    downloaded = telegram_runtime_module._download_photo_from_url("https://basket-41.wbbasket.ru/item.webp")

    assert downloaded.data == b"image-bytes"
    assert downloaded.content_type == "application/octet-stream"


def test_convert_image_bytes_to_jpeg_accepts_real_webp_bytes() -> None:
    source = io.BytesIO()
    Image.new("RGBA", (2, 2), (200, 20, 30, 180)).save(source, format="WEBP")

    jpeg_data = telegram_runtime_module._convert_image_bytes_to_jpeg(source.getvalue())

    with Image.open(io.BytesIO(jpeg_data)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (2, 2)


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


def test_runtime_caches_stateless_flow_factories_and_refreshes_display_rate() -> None:
    runtime = _build_runtime()

    assert runtime._seller_withdrawal_creation_flow() is runtime._seller_withdrawal_creation_flow()
    assert runtime._buyer_withdrawal_creation_flow() is runtime._buyer_withdrawal_creation_flow()
    assert runtime._admin_exceptions_flow() is runtime._admin_exceptions_flow()

    seller_flow = runtime._get_seller_listing_creation_flow()
    assert runtime._get_seller_listing_creation_flow() is seller_flow
    assert "~100" in seller_flow.instruction_text(shop_title="Тушенка")

    buyer_flow = runtime._buyer_marketplace_flow()
    assert runtime._buyer_marketplace_flow() is buyer_flow

    runtime._display_rub_per_usdt = Decimal("101")
    refreshed_seller_flow = runtime._get_seller_listing_creation_flow()
    refreshed_buyer_flow = runtime._buyer_marketplace_flow()

    assert refreshed_seller_flow is not seller_flow
    assert "~101" in refreshed_seller_flow.instruction_text(shop_title="Тушенка")
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
async def test_runtime_uploads_webp_photo_url_from_memory_without_direct_url_attempt() -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    runtime._download_photo_for_upload = AsyncMock(
        return_value=_DownloadedPhoto(
            data=b"webp-bytes",
            content_type="image/webp",
            final_url="https://basket-41.wbbasket.ru/item.webp",
        )
    )
    transport = FakeTransport()
    message = FakeMessage(transport=transport, chat=FakeChat(transport=transport, chat_id=100))

    await runtime._reply_with_photo_if_available(message, photo_url="https://basket-41.wbbasket.ru/item.webp")

    assert [event.kind for event in transport.events] == ["reply_photo"]
    uploaded = transport.events[0].photo
    assert isinstance(uploaded, InputFile)
    assert uploaded.filename == "listing.webp"
    assert uploaded.input_file_content == b"webp-bytes"
    runtime._logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_falls_back_to_memory_upload_when_direct_photo_url_fails() -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    runtime._download_photo_for_upload = AsyncMock(
        return_value=_DownloadedPhoto(
            data=b"jpeg-bytes",
            content_type="image/jpeg",
            final_url="https://basket-41.wbbasket.ru/item.jpg",
        )
    )
    transport = FakeTransport()
    message = FailingPhotoMessage(
        failures=1,
        transport=transport,
        chat=FakeChat(transport=transport, chat_id=100),
    )

    await runtime._reply_with_photo_if_available(message, photo_url="https://basket-41.wbbasket.ru/item.jpg")

    assert [event.kind for event in transport.events] == ["reply_photo"]
    uploaded = transport.events[0].photo
    assert isinstance(uploaded, InputFile)
    assert uploaded.filename == "listing.jpg"
    assert uploaded.input_file_content == b"jpeg-bytes"
    runtime._logger.warning.assert_called_once()
    assert runtime._logger.warning.call_args.kwargs["strategy"] == "direct_url"


@pytest.mark.asyncio
async def test_runtime_does_not_download_untrusted_photo_url_when_direct_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    blocked_download = Mock(side_effect=AssertionError("untrusted URL must not be downloaded"))
    monkeypatch.setattr(telegram_runtime_module, "_download_photo_from_url", blocked_download)
    transport = FakeTransport()
    message = FailingPhotoMessage(
        failures=1,
        transport=transport,
        chat=FakeChat(transport=transport, chat_id=100),
    )

    await runtime._reply_with_photo_if_available(message, photo_url="https://cdn.example/item.jpg")

    assert transport.events == []
    blocked_download.assert_not_called()
    runtime._logger.warning.assert_called_once()
    assert runtime._logger.warning.call_args.kwargs["strategy"] == "direct_url"


@pytest.mark.asyncio
async def test_runtime_converts_downloaded_photo_to_jpeg_when_upload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    runtime._download_photo_for_upload = AsyncMock(
        return_value=_DownloadedPhoto(
            data=b"webp-bytes",
            content_type="image/webp",
            final_url="https://basket-41.wbbasket.ru/item.webp",
        )
    )
    monkeypatch.setattr(telegram_runtime_module, "_convert_image_bytes_to_jpeg", lambda data: b"jpeg-bytes")
    transport = FakeTransport()
    message = FailingPhotoMessage(
        failures=1,
        transport=transport,
        chat=FakeChat(transport=transport, chat_id=100),
    )

    await runtime._reply_with_photo_if_available(message, photo_url="https://basket-41.wbbasket.ru/item.webp")

    assert [event.kind for event in transport.events] == ["reply_photo"]
    uploaded = transport.events[0].photo
    assert isinstance(uploaded, InputFile)
    assert uploaded.filename == "listing.jpg"
    assert uploaded.input_file_content == b"jpeg-bytes"
    runtime._logger.warning.assert_called_once()
    assert runtime._logger.warning.call_args.kwargs["strategy"] == "memory_upload"


@pytest.mark.asyncio
async def test_runtime_keeps_text_screen_when_photo_download_fails() -> None:
    runtime = _build_runtime()
    runtime._logger = SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock())
    transport = FakeTransport()
    chat = FakeChat(transport=transport, chat_id=100)
    message = FakeMessage(transport=transport, chat=chat)
    context = FakeContext(bot=FakeBot(transport=transport))
    original_download = telegram_runtime_module._download_photo_from_url
    telegram_runtime_module._download_photo_from_url = Mock(side_effect=_PhotoDownloadError("blocked"))
    try:
        await runtime._apply_transport_effects(
            context=context,
            query_message=None,
            message=message,
            default_role="buyer",
            result=FlowResult(
                effects=(
                    ReplyPhoto(photo_url="https://basket-41.wbbasket.ru/item.webp"),
                    ReplyText(text="Карточка товара"),
                )
            ),
        )
    finally:
        telegram_runtime_module._download_photo_from_url = original_download

    assert [event.kind for event in transport.events] == ["reply"]
    assert transport.events[0].text == "Карточка товара"
    runtime._logger.warning.assert_called_once()
    assert runtime._logger.warning.call_args.args[0] == "telegram_photo_download_failed"


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
