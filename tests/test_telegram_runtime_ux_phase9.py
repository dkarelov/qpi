from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from libs.config.settings import BotApiSettings
from libs.domain.public_refs import (
    build_support_deep_link,
    format_assignment_ref,
    format_chain_tx_ref,
    format_deposit_ref,
    format_listing_ref,
    format_shop_ref,
    format_withdrawal_ref,
)
from services.bot_api.buyer_marketplace_flow import (
    buyer_dashboard_status_bucket,
    buyer_purchase_status_badge,
    buyer_review_instruction_text,
    buyer_task_instruction_text,
)
from services.bot_api.presentation import (
    buyer_listing_detail_html,
    format_buyer_cashback_with_percent,
    format_cashback_with_percent,
    format_copyable_code,
    format_usdt,
    format_usdt_with_rub,
    screen_text,
)
from services.bot_api.seller_listing_creation_flow import SellerListingCreationFlow
from services.bot_api.seller_marketplace_flow import SellerMarketplaceFlow, SellerMarketplaceFlowConfig
from services.bot_api.telegram_runtime import TelegramWebhookRuntime
from services.bot_api.ton_links import build_ton_usdt_transfer_link
from services.bot_api.transport_effects import ReplaceText

_TASK_UUID = "11111111-1111-4111-8111-111111111111"


def _build_runtime(*, support_bot_username: str | None = "qpilka_support_bot") -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [1],
            "DISPLAY_RUB_PER_USDT": "100",
            "SUPPORT_BOT_USERNAME": support_bot_username,
        }
    )
    return TelegramWebhookRuntime(settings=settings)


def _flatten_labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _flatten_button_labels(rows) -> list[str]:
    return [button.text for row in rows for button in row]


def _seller_listing_creation_flow() -> SellerListingCreationFlow:
    return SellerListingCreationFlow(
        seller_service=object(),  # type: ignore[arg-type]
        seller_workflow=object(),  # type: ignore[arg-type]
        display_rub_per_usdt=Decimal("100"),
    )


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


class _SellerFlowSellerService:
    def __init__(
        self,
        *,
        shops: list[Any] | None = None,
        listings: list[Any] | None = None,
        balance: Any | None = None,
    ) -> None:
        self.shops = shops or []
        self.listings = listings or []
        self.balance = balance or _ns(
            seller_available_usdt=Decimal("10.000000"),
            seller_collateral_usdt=Decimal("0.000000"),
            seller_withdraw_pending_usdt=Decimal("0.000000"),
        )

    async def list_shops(self, *, seller_user_id: int) -> list[Any]:
        return self.shops

    async def get_shop(self, *, seller_user_id: int, shop_id: int) -> Any:
        return next(shop for shop in self.shops if shop.shop_id == shop_id)

    async def list_listing_collateral_views(self, *, seller_user_id: int) -> list[Any]:
        return self.listings

    async def get_listing(self, *, seller_user_id: int, listing_id: int) -> Any:
        return next(listing for listing in self.listings if listing.listing_id == listing_id)

    async def get_seller_balance_snapshot(self, *, seller_user_id: int) -> Any:
        return self.balance

    async def get_seller_order_counters(self, *, seller_user_id: int) -> dict[str, int]:
        return {"awaiting_order": 0, "ordered": 0, "picked_up": 0}


class _SellerFlowFinanceService:
    async def get_active_seller_withdrawal_request(self, *, seller_user_id: int) -> Any | None:
        return None


class _SellerFlowDepositService:
    pass


class _SellerFlowWorkflow:
    async def activate_listing(self, *, seller_user_id: int, listing_id: int, idempotency_key: str) -> Any:
        raise AssertionError("seller workflow is not exercised in these tests")

    async def unpause_listing(self, *, seller_user_id: int, listing_id: int) -> Any:
        raise AssertionError("seller workflow is not exercised in these tests")


def _seller_marketplace_flow(
    *,
    support_bot_username: str | None = "qpilka_support_bot",
    seller_service: _SellerFlowSellerService | None = None,
) -> SellerMarketplaceFlow:
    return SellerMarketplaceFlow(
        seller_service=seller_service or _SellerFlowSellerService(),
        seller_workflow=_SellerFlowWorkflow(),
        finance_service=_SellerFlowFinanceService(),
        deposit_service=_SellerFlowDepositService(),
        wb_ping_client=None,
        listing_creation_flow=_seller_listing_creation_flow(),
        config=SellerMarketplaceFlowConfig(
            display_rub_per_usdt=Decimal("100"),
            telegram_bot_username="qpilka_bot",
            token_cipher_key="test-key",
            seller_collateral_shard_key="mvp-1",
            seller_collateral_invoice_ttl_hours=24,
            tonapi_usdt_jetton_master="jetton-master",
            telegram_wallet_open_url="https://t.me/wallet/start",
            support_bot_username=support_bot_username,
        ),
    )


def _starts_with_emoji(label: str) -> bool:
    if not label:
        return False
    return ord(label[0]) > 127


def test_seller_menu_is_tree_structured() -> None:
    labels = _flatten_button_labels(_seller_marketplace_flow().menu_buttons())
    labels_set = set(labels)

    assert "🏬 Магазины" in labels_set
    assert "📦 Объявления" in labels_set
    assert "💰 Баланс" in labels_set
    assert "📘 Инструкция" in labels_set
    assert "➕ Создать магазин" not in labels_set
    assert "➕ Создать объявление" not in labels_set
    assert "➕ Пополнить" not in labels_set


def test_seller_menu_puts_listings_before_shops() -> None:
    first_row = _seller_marketplace_flow().menu_buttons()[0]

    assert [button.text for button in first_row] == ["📦 Объявления", "🏬 Магазины"]


def test_counted_button_labels_use_middot_suffix() -> None:
    runtime = _build_runtime()

    seller_labels = _flatten_button_labels(_seller_marketplace_flow().menu_buttons(listings_count=3, shops_count=2))
    admin_labels = _flatten_labels(
        runtime._admin_menu_markup(
            pending_withdrawals_count=4,
            deposit_exceptions_count=5,
            exceptions_count=6,
        )
    )

    assert "📦 Объявления · 3" in seller_labels
    assert "🏬 Магазины · 2" in seller_labels
    assert "💸 Выводы · 4" in admin_labels
    assert "🏦 Депозиты · 5" in admin_labels
    assert "⚠️ Исключения · 6" in admin_labels


def test_buyer_menu_is_dashboard_sections() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._buyer_menu_markup())
    labels_set = set(labels)

    assert "🏪 Магазины" in labels_set
    assert "📋 Покупки" in labels_set
    assert "💳 Баланс и вывод" in labels_set
    assert "📘 Инструкция" in labels_set
    assert "🆘 Поддержка" not in labels_set


def test_admin_menu_is_dashboard_sections() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._admin_menu_markup())
    labels_set = set(labels)

    assert "💸 Выводы" in labels_set
    assert "🏦 Депозиты" in labels_set
    assert "⚠️ Исключения" in labels_set
    assert "🏦 Ручной депозит" not in labels_set


def test_root_and_role_menus_use_emoji_labels() -> None:
    runtime = _build_runtime()
    root_labels = _flatten_labels(runtime._root_menu_markup(identity=None))
    seller_labels = _flatten_button_labels(_seller_marketplace_flow().menu_buttons())
    buyer_labels = _flatten_labels(runtime._buyer_menu_markup())
    admin_labels = _flatten_labels(runtime._admin_menu_markup())

    for label in root_labels + seller_labels + buyer_labels + admin_labels:
        assert _starts_with_emoji(label), label


def test_role_menus_do_not_have_switch_role_button() -> None:
    runtime = _build_runtime()

    seller_labels = _flatten_button_labels(_seller_marketplace_flow().menu_buttons())
    buyer_labels = _flatten_labels(runtime._buyer_menu_markup())
    admin_labels = _flatten_labels(runtime._admin_menu_markup())

    assert "🔄 Сменить роль" not in set(seller_labels)
    assert "🔄 Сменить роль" not in set(buyer_labels)
    assert "🔄 Сменить роль" not in set(admin_labels)


def test_seller_shop_detail_menu_is_structured() -> None:
    flow = _seller_marketplace_flow(
        seller_service=_SellerFlowSellerService(
            shops=[_ns(shop_id=1, title="Магазин", slug="shop", wb_token_status="invalid")]
        )
    )

    result = asyncio.run(flow.render_shop_details(seller_user_id=101, shop_id=1))
    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    labels = _flatten_button_labels(screen.buttons)
    labels_set = set(labels)

    assert "❌ Токен WB API" in labels_set
    assert "✏️ Переименовать" in labels_set
    assert "🗑 Удалить" in labels_set
    assert "↩️ К списку магазинов" in labels_set
    assert "📘 Про магазины" in labels_set
    assert "🧭 Дашборд продавца" not in labels_set


def test_seller_shop_detail_token_button_shows_valid_state() -> None:
    flow = _seller_marketplace_flow(
        seller_service=_SellerFlowSellerService(
            shops=[_ns(shop_id=1, title="Магазин", slug="shop", wb_token_status="valid")]
        )
    )

    result = asyncio.run(flow.render_shop_details(seller_user_id=101, shop_id=1))
    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)
    labels = _flatten_button_labels(screen.buttons)

    assert "✅ Токен WB API" in labels


def test_shop_create_button_starts_with_token_step() -> None:
    result = asyncio.run(_seller_marketplace_flow().render_shops(seller_user_id=101))
    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)

    create_shop_button = screen.buttons[0][0]

    assert create_shop_button.flow == "seller"
    assert create_shop_button.action == "shop_create_token_prompt"


def test_seller_balance_menu_uses_transactions_and_kb_labels() -> None:
    result = asyncio.run(_seller_marketplace_flow().render_balance(seller_user_id=101))
    screen = result.effects[0]
    assert isinstance(screen, ReplaceText)

    labels = _flatten_button_labels(screen.buttons)
    labels_set = set(labels)

    assert "🧾 Транзакции" in labels_set
    assert "↩️ Назад" in labels_set
    assert "📘 Про баланс и вывод" in labels_set
    assert "🧾 Мои пополнения / Проверить" not in labels_set


def test_money_formatter_uses_usdt_with_approx_rub() -> None:
    assert format_usdt_with_rub(Decimal("1.24"), display_rub_per_usdt=Decimal("100")) == "$1.2 (~124 ₽)"
    assert format_usdt_with_rub(Decimal("1.25"), display_rub_per_usdt=Decimal("100")) == "$1.3 (~125 ₽)"
    assert format_usdt_with_rub(Decimal("0"), display_rub_per_usdt=Decimal("100")) == "$0.0"
    assert format_usdt(Decimal("1.234567"), precise=True) == "$1.234567"


def test_buyer_cashback_formatter_uses_approx_rub() -> None:
    assert (
        format_buyer_cashback_with_percent(
            reward_usdt=Decimal("1.29"),
            reference_price_rub=None,
            display_rub_per_usdt=Decimal("100"),
        )
        == "~129 ₽"
    )
    assert (
        format_buyer_cashback_with_percent(
            reward_usdt=Decimal("1.20"),
            reference_price_rub=None,
            display_rub_per_usdt=Decimal("100"),
        )
        == "~120 ₽"
    )
    assert (
        format_buyer_cashback_with_percent(
            reward_usdt=Decimal("0"),
            reference_price_rub=None,
            display_rub_per_usdt=Decimal("100"),
        )
        == "~0 ₽"
    )


def test_buyer_listing_token_contains_search_phrase_product_count_and_brand() -> None:
    assignment = type(
        "Assignment",
        (),
        {
            "display_title": "Бумага A4",
            "search_phrase": "бумага а4 для принтера 500 листов белая",
            "task_uuid": _TASK_UUID,
            "wb_product_id": 552892532,
            "wb_brand_name": "BRAUBERG",
            "reservation_expires_at": datetime(2026, 4, 4, 3, 31, tzinfo=UTC),
        },
    )()
    text = buyer_task_instruction_text(assignment, include_title=False)
    token = text.split("<code>", maxsplit=1)[1].split("</code>", maxsplit=1)[0]
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert decoded == [1, _TASK_UUID, "бумага а4 для принтера 500 листов белая", 552892532, 1, "BRAUBERG"]


def test_token_instruction_contains_required_sections() -> None:
    text = _seller_marketplace_flow().shop_token_instruction_text(shop_title="Мой магазин")
    assert "Токен WB API для магазина" in text
    assert "Отправьте токен WB API следующим сообщением ниже." in text
    assert "Создайте Базовый токен в режиме «Только для чтения»" in text
    assert "Контент, Статистика, Вопросы и отзывы" in text
    assert "получения информации о товаре, проверки статуса заказов и отзывов" in text
    assert "изменить данные с ним невозможно" in text


def test_listing_create_instruction_contains_new_fields_and_fx_reference() -> None:
    text = _seller_listing_creation_flow().instruction_text(shop_title="Тушенка")
    assert "Создание объявления для магазина «Тушенка»" in text
    assert "<i>Отправьте данные объявления одним сообщением в формате ниже.</i>" in text
    assert ("артикул ВБ, кэшбэк в рублях, макс. заказов, поисковая фраза") in text
    assert "фраза для отзыва 1" in text
    assert "12345678, 100, 5, женские джинсы" in text
    assert "бот зафиксирует ее в USDT" in text
    assert "~100" in text
    assert "Фразы для отзыва" in text
    assert "загрузит карточку WB" in text
    assert "определит цену покупателя" in text


def test_screen_text_places_cta_after_title_and_separates_lines() -> None:
    text = screen_text(
        title="Экран",
        cta="Сделайте следующий шаг.",
        lines=["Первый блок", "Второй блок"],
        note="Подсказка внизу.",
    )

    assert text.startswith("<b>Экран</b>\n\n<i>Сделайте следующий шаг.</i>")
    assert "Первый блок\nВторой блок" in text
    assert text.endswith("<i>Подсказка внизу.</i>")


def test_screen_text_supports_ref_suffix_outside_bold_title() -> None:
    text = screen_text(
        title="Магазин",
        title_suffix_html=" · <code>S4</code>",
    )

    assert text == "<b>🏪 Магазин</b> · <code>S4</code>"


def test_listing_created_prompt_activation_explains_activation_effect() -> None:
    text = _seller_listing_creation_flow().created_prompt_activation_text(
        display_title="Джинсы женские прямые",
        wb_product_id=12345678,
        wb_subject_name="Джинсы",
        wb_vendor_code="sku-1",
        wb_source_title="LeBrand Джинсы женские прямые",
        wb_brand_name="LeBrand",
        reference_price_rub=1200,
        reference_price_source="orders",
        search_phrase="женские джинсы",
        review_phrases=["в размер", "не садятся после стирки"],
        cashback_rub=Decimal("100"),
        reward_usdt=Decimal("1.000000"),
        slot_count=5,
        collateral_required_usdt=Decimal("5.050000"),
    )

    assert "Активировать объявление сейчас?" in text
    assert "отправьте покупателям ссылку на товар" in text
    assert "Товар:</b> Джинсы женские прямые" in text
    assert "Источник цены:</b> рассчитана по заказам за 30 дней." in text
    assert "Артикул продавца:</b> sku-1" in text
    assert "Цена покупателя:</b> 1200 ₽" in text
    assert "Фразы для отзыва:</b> в размер; не садятся после стирки" in text


def test_cashback_rub_formatter_includes_percent_when_reference_price_is_known() -> None:
    assert (
        format_cashback_with_percent(
            reward_usdt=Decimal("1.000000"),
            reference_price_rub=1200,
            display_rub_per_usdt=Decimal("100"),
        )
        == "$1.0 (~100 ₽, ~8%)"
    )


def test_buyer_task_instruction_contains_title_link_and_deadline() -> None:
    assignment = type(
        "Assignment",
        (),
        {
            "display_title": "Джинсы женские прямые",
            "search_phrase": "женские джинсы",
            "task_uuid": _TASK_UUID,
            "wb_product_id": 12345678,
            "wb_brand_name": "LeBrand",
            "reservation_expires_at": datetime(2026, 4, 4, 3, 31, tzinfo=UTC),
        },
    )()
    text = buyer_task_instruction_text(assignment)
    token = text.split("<code>", maxsplit=1)[1].split("</code>", maxsplit=1)[0]
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert "<b>Товар:</b> Джинсы женские прямые" in text
    assert (
        '<a href="https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga">'
        "расширении для браузера Chrome / Яндекс Qpilka</a>"
    ) in text
    assert "до 04.04.2026 06:31 МСК (по истечении срока бронь отменится)." in text
    assert "Отправьте токен-подтверждение сюда." in text
    assert "Поисковая фраза:</b>" not in text
    assert decoded == [1, _TASK_UUID, "женские джинсы", 12345678, 1, "LeBrand"]


def test_buyer_listing_detail_hides_singleton_zero_size() -> None:
    listing = type(
        "Listing",
        (),
        {
            "listing_id": 21,
            "display_title": "Белорусская тушенка",
            "search_phrase": "тушенка белорусская",
            "wb_subject_name": "Консервы",
            "reference_price_rub": 490,
            "reward_usdt": Decimal("1.250000"),
            "wb_tech_sizes": ["0"],
            "wb_description": "Говядина",
            "wb_characteristics": [{"name": "Вес", "value": "325 г"}],
        },
    )()

    text = buyer_listing_detail_html(listing=listing, display_rub_per_usdt=Decimal("100"))

    assert "<b>Размеры:</b>" not in text
    assert "Характеристики" in text


def test_buyer_review_instruction_contains_token_and_selected_phrases() -> None:
    assignment = type(
        "Assignment",
        (),
        {
            "display_title": "Джинсы женские прямые",
            "search_phrase": "женские джинсы",
            "task_uuid": _TASK_UUID,
            "wb_product_id": 12345678,
            "review_phrases": ["в размер", "не садятся после стирки"],
        },
    )()
    text = buyer_review_instruction_text(assignment)
    token = text.split("<code>", maxsplit=1)[1].split("</code>", maxsplit=1)[0]
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert "Скопируйте токен ниже в расширение Qpilka." in text
    assert "Расширение покажет, какой отзыв оставить на WB." in text
    assert "Поставьте 5 звезд и добавьте обязательные фразы." in text
    assert "После публикации расширение выдаст токен-подтверждение." in text
    assert "Вернитесь сюда и нажмите кнопку ниже." in text
    assert "Обязательные фразы:</b> в размер; не садятся после стирки" in text
    assert decoded == [2, _TASK_UUID, 12345678, "в размер", "не садятся после стирки"]


def test_buyer_review_status_stays_in_yellow_bucket() -> None:
    assert buyer_dashboard_status_bucket("picked_up_wait_review") == "ordered"
    assert "Нужно оставить отзыв" in buyer_purchase_status_badge("picked_up_wait_review")


def test_wallet_link_builder_uses_ton_transfer_with_usdt_jetton_and_micro_units() -> None:
    flow = _seller_marketplace_flow()

    link = flow._build_ton_usdt_wallet_link(
        destination_address="UQTESTADDRESS",
        expected_amount_usdt=Decimal("1.200100"),
        text="QPI deposit D91",
    )

    assert link.startswith("ton://transfer/UQTESTADDRESS?")
    assert "jetton=jetton-master" in link
    assert "amount=1200100" in link
    assert "text=QPI+deposit+D91" in link


def test_ton_usdt_transfer_link_helper_uses_micro_units_and_encoded_memo() -> None:
    link = build_ton_usdt_transfer_link(
        destination_address=" UQTESTADDRESS ",
        amount_usdt=Decimal("5.000000"),
        jetton_master="jetton-master",
        text="QPI withdrawal W77",
    )

    assert link.startswith("ton://transfer/UQTESTADDRESS?")
    assert "jetton=jetton-master" in link
    assert "amount=5000000" in link
    assert "text=QPI+withdrawal+W77" in link


def test_ton_usdt_transfer_link_helper_rejects_empty_jetton_master() -> None:
    with pytest.raises(ValueError, match="jetton_master must not be empty"):
        build_ton_usdt_transfer_link(
            destination_address="UQTESTADDRESS",
            amount_usdt=Decimal("5.000000"),
            jetton_master=" ",
        )


def test_telegram_wallet_link_builder_uses_wallet_start_url() -> None:
    runtime = _build_runtime()

    assert runtime._build_telegram_wallet_open_link() == "https://t.me/wallet/start"


def test_public_ref_formatters_use_short_prefixed_ids() -> None:
    assert format_shop_ref(11) == "S11"
    assert format_listing_ref(21) == "L21"
    assert format_assignment_ref(31) == "P31"
    assert format_withdrawal_ref(77) == "W77"
    assert format_deposit_ref(91) == "D91"
    assert format_chain_tx_ref(11) == "TX11"


def test_copyable_code_helper_wraps_value_in_html_code() -> None:
    assert format_copyable_code("UQ_TEST") == "<code>UQ_TEST</code>"


def test_support_link_builder_uses_support_bot_and_context_fallback() -> None:
    runtime = _build_runtime()

    assert (
        runtime._build_support_link(
            role="buyer",
            topic="purchase",
            refs=["P31", "L21", "S11"],
        )
        == "https://t.me/qpilka_support_bot?start=buyer_purchase_P31_L21_S11"
    )
    assert (
        build_support_deep_link(
            bot_username="qpilka_support_bot",
            role="seller",
            topic="listing",
            refs=["L" + "1" * 80],
        )
        == "https://t.me/qpilka_support_bot?start=seller_generic"
    )


def test_support_buttons_are_hidden_when_support_bot_username_is_missing() -> None:
    runtime = _build_runtime(support_bot_username=None)

    assert "🆘 Поддержка" not in set(
        _flatten_button_labels(_seller_marketplace_flow(support_bot_username=None).menu_buttons())
    )
    assert "🆘 Поддержка" not in set(_flatten_labels(runtime._buyer_menu_markup()))


def test_seller_dashboard_keeps_support_but_buyer_dashboard_hides_it() -> None:
    runtime = _build_runtime()

    assert "🆘 Поддержка" in set(_flatten_button_labels(_seller_marketplace_flow().menu_buttons()))
    assert "🆘 Поддержка" not in set(_flatten_labels(runtime._buyer_menu_markup()))


def test_seller_listing_detail_markup_hides_edit_button_when_activation_is_blocked() -> None:
    listing = _ns(
        listing_id=21,
        shop_id=11,
        display_title="Бумага A4 для принтера",
        reference_price_rub=400,
        reference_price_source="orders",
        wb_photo_url=None,
        wb_product_id=552892532,
        search_phrase="бумага а4 для принтера",
        status="draft",
        reward_usdt=Decimal("1.000000"),
        available_slots=5,
        slot_count=5,
        in_progress_assignments_count=0,
        collateral_locked_usdt=Decimal("0.000000"),
        collateral_required_usdt=Decimal("5.050000"),
        reserved_slot_usdt=Decimal("0.000000"),
        wb_subject_name="Бумага офисная",
        wb_vendor_code="paper-001",
        wb_brand_name="BRAUBERG",
        wb_source_title="BRAUBERG Бумага A4 для принтера",
        wb_description=None,
        wb_tech_sizes=[],
        wb_characteristics=[],
        review_phrases=[],
    )
    flow = _seller_marketplace_flow(
        seller_service=_SellerFlowSellerService(
            listings=[listing],
            balance=_ns(
                seller_available_usdt=Decimal("0.000000"),
                seller_collateral_usdt=Decimal("0.000000"),
                seller_withdraw_pending_usdt=Decimal("0.000000"),
            ),
        )
    )

    result = asyncio.run(flow.render_listing_detail(seller_user_id=101, listing_id=21))
    screen = next(effect for effect in result.effects if isinstance(effect, ReplaceText))
    labels = _flatten_button_labels(screen.buttons)

    assert "✏️ Редактировать" not in labels
    assert "⛔ Недостаточно средств" in labels
