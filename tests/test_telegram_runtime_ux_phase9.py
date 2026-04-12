from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal

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
from services.bot_api.callback_data import build_callback
from services.bot_api.telegram_runtime import TelegramWebhookRuntime

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


def _starts_with_emoji(label: str) -> bool:
    if not label:
        return False
    return ord(label[0]) > 127


def test_seller_menu_is_tree_structured() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_menu_markup())
    labels_set = set(labels)

    assert "🏬 Магазины" in labels_set
    assert "📦 Объявления" in labels_set
    assert "💰 Баланс" in labels_set
    assert "📘 Инструкция" in labels_set
    assert "➕ Создать магазин" not in labels_set
    assert "➕ Создать объявление" not in labels_set
    assert "➕ Пополнить" not in labels_set


def test_seller_menu_puts_listings_before_shops() -> None:
    runtime = _build_runtime()

    first_row = runtime._seller_menu_markup().inline_keyboard[0]

    assert [button.text for button in first_row] == ["📦 Объявления", "🏬 Магазины"]


def test_counted_button_labels_use_plain_suffix_options() -> None:
    runtime = _build_runtime()

    seller_labels = _flatten_labels(runtime._seller_menu_markup(listings_count=3, shops_count=2))
    admin_labels = _flatten_labels(
        runtime._admin_menu_markup(
            pending_withdrawals_count=4,
            deposit_exceptions_count=5,
            exceptions_count=6,
        )
    )

    assert "📦 Объявления [3]" in seller_labels
    assert "🏬 Магазины [2]" in seller_labels
    assert "💸 Выводы [4]" in admin_labels
    assert "🏦 Депозиты · 5" in admin_labels
    assert "⚠️ Исключения (6)" in admin_labels


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
    seller_labels = _flatten_labels(runtime._seller_menu_markup())
    buyer_labels = _flatten_labels(runtime._buyer_menu_markup())
    admin_labels = _flatten_labels(runtime._admin_menu_markup())

    for label in root_labels + seller_labels + buyer_labels + admin_labels:
        assert _starts_with_emoji(label), label


def test_role_menus_do_not_have_switch_role_button() -> None:
    runtime = _build_runtime()

    seller_labels = _flatten_labels(runtime._seller_menu_markup())
    buyer_labels = _flatten_labels(runtime._buyer_menu_markup())
    admin_labels = _flatten_labels(runtime._admin_menu_markup())

    assert "🔄 Сменить роль" not in set(seller_labels)
    assert "🔄 Сменить роль" not in set(buyer_labels)
    assert "🔄 Сменить роль" not in set(admin_labels)


def test_seller_shop_detail_menu_is_structured() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_shop_detail_markup(shop_id=1, token_is_valid=False))
    labels_set = set(labels)

    assert "❌ Токен WB API" in labels_set
    assert "✏️ Переименовать" in labels_set
    assert "🗑 Удалить" in labels_set
    assert "↩️ К списку магазинов" in labels_set
    assert "📘 Про магазины" in labels_set
    assert "🧭 Дашборд продавца" not in labels_set


def test_seller_shop_detail_token_button_shows_valid_state() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_shop_detail_markup(shop_id=1, token_is_valid=True))

    assert "✅ Токен WB API" in labels


def test_shop_create_button_starts_with_token_step() -> None:
    runtime = _build_runtime()

    markup = runtime._seller_shops_menu_markup(has_shops=True)
    create_shop_button = markup.inline_keyboard[0][0]

    assert create_shop_button.callback_data == build_callback(
        flow="seller",
        action="shop_create_token_prompt",
    )


def test_seller_balance_menu_uses_transactions_and_kb_labels() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_balance_menu_markup())
    labels_set = set(labels)

    assert "🧾 Транзакции" in labels_set
    assert "↩️ Назад" in labels_set
    assert "📘 Про баланс и вывод" in labels_set
    assert "🧾 Мои пополнения / Проверить" not in labels_set


def test_money_formatter_uses_usdt_with_approx_rub() -> None:
    runtime = _build_runtime()

    assert runtime._format_usdt_with_rub(Decimal("1.24")) == "$1.2 (~124 ₽)"
    assert runtime._format_usdt_with_rub(Decimal("1.25")) == "$1.3 (~125 ₽)"
    assert runtime._format_usdt_with_rub(Decimal("0")) == "$0.0"
    assert runtime._format_usdt(Decimal("1.234567"), precise=True) == "$1.234567"


def test_buyer_cashback_formatter_uses_approx_rub() -> None:
    runtime = _build_runtime()

    assert runtime._format_buyer_listing_cashback(Decimal("1.29")) == "~129 ₽"
    assert runtime._format_buyer_listing_cashback(Decimal("1.20")) == "~120 ₽"
    assert runtime._format_buyer_listing_cashback(Decimal("0")) == "~0 ₽"


def test_buyer_listing_token_contains_search_phrase_product_count_and_brand() -> None:
    runtime = _build_runtime()

    token = runtime._build_buyer_listing_token(
        task_uuid=_TASK_UUID,
        search_phrase="бумага а4 для принтера 500 листов белая",
        wb_product_id=552892532,
        brand_name="BRAUBERG",
    )
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert decoded == [1, _TASK_UUID, "бумага а4 для принтера 500 листов белая", 552892532, 1, "BRAUBERG"]


def test_token_instruction_contains_required_sections() -> None:
    runtime = _build_runtime()

    text = runtime._shop_token_instruction_text(shop_title="Мой магазин")
    assert "Токен WB API для магазина" in text
    assert "Отправьте токен WB API следующим сообщением ниже." in text
    assert "Создайте Базовый токен в режиме «Только для чтения»" in text
    assert "Контент, Статистика, Вопросы и отзывы" in text
    assert "получения информации о товаре, проверки статуса заказов и отзывов" in text
    assert "изменить данные с ним невозможно" in text


def test_listing_create_instruction_contains_new_fields_and_fx_reference() -> None:
    runtime = _build_runtime()

    text = runtime._listing_create_instruction_text(shop_title="Тушенка")
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
    runtime = _build_runtime()

    text = runtime._screen_text(
        title="Экран",
        cta="Сделайте следующий шаг.",
        lines=["Первый блок", "Второй блок"],
        note="Подсказка внизу.",
    )

    assert text.startswith("<b>Экран</b>\n\n<i>Сделайте следующий шаг.</i>")
    assert "Первый блок\nВторой блок" in text
    assert text.endswith("<i>Подсказка внизу.</i>")


def test_screen_text_supports_ref_suffix_outside_bold_title() -> None:
    runtime = _build_runtime()

    text = runtime._screen_text(
        title="Магазин",
        title_suffix_html=" · <code>S4</code>",
    )

    assert text == "<b>🏪 Магазин</b> · <code>S4</code>"


def test_listing_created_prompt_activation_explains_activation_effect() -> None:
    runtime = _build_runtime()

    text = runtime._listing_created_prompt_activation_text(
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
    assert "отправьте покупателям ссылку на магазин" in text
    assert "Товар:</b> Джинсы женские прямые" in text
    assert "Источник цены:</b> рассчитана по заказам за 30 дней." in text
    assert "Артикул продавца:</b> sku-1" in text
    assert "Цена покупателя:</b> 1200 ₽" in text
    assert "Фразы для отзыва:</b> в размер; не садятся после стирки" in text


def test_cashback_rub_formatter_includes_percent_when_reference_price_is_known() -> None:
    runtime = _build_runtime()

    assert (
        runtime._format_cashback_rub_with_percent(
            reward_usdt=Decimal("1.000000"),
            reference_price_rub=1200,
        )
        == "$1.0 (~100 ₽, ~8%)"
    )


def test_buyer_task_instruction_contains_title_link_and_deadline() -> None:
    runtime = _build_runtime()

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
    text = runtime._buyer_task_instruction_text(assignment)
    token = text.split("<code>", maxsplit=1)[1].split("</code>", maxsplit=1)[0]
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert "<b>Товар:</b> Джинсы женские прямые" in text
    assert (
        '<a href="https://chromewebstore.google.com/detail/qpilka/joefinmgneknnaejambgbaclobeedaga">'
        "расширении для браузера Chrome / Яндекс Qpilka</a>"
    ) in text
    assert "до 04.04.2026 06:31 MSK (по истечении срока бронь отменится)." in text
    assert "Отправьте токен-подтверждение сюда." in text
    assert "Поисковая фраза:</b>" not in text
    assert decoded == [1, _TASK_UUID, "женские джинсы", 12345678, 1, "LeBrand"]


def test_buyer_listing_detail_hides_singleton_zero_size() -> None:
    runtime = _build_runtime()
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

    text = runtime._buyer_listing_detail_html(listing=listing)

    assert "<b>Размеры:</b>" not in text
    assert "Характеристики" in text


def test_buyer_review_instruction_contains_token_and_selected_phrases() -> None:
    runtime = _build_runtime()

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
    text = runtime._buyer_review_instruction_text(assignment)
    token = text.split("<code>", maxsplit=1)[1].split("</code>", maxsplit=1)[0]
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    assert "оставьте отзыв на 5 звезд" in text
    assert "Фразы для отзыва:</b> в размер; не садятся после стирки" in text
    assert decoded == [2, _TASK_UUID, 12345678, "в размер", "не садятся после стирки"]


def test_buyer_review_status_stays_in_yellow_bucket() -> None:
    runtime = _build_runtime()

    assert runtime._buyer_dashboard_status_bucket("picked_up_wait_review") == "ordered"
    assert "Нужно оставить отзыв" in runtime._buyer_purchase_status_badge("picked_up_wait_review")


def test_wallet_link_builder_uses_ton_transfer_with_usdt_jetton_and_micro_units() -> None:
    runtime = _build_runtime()

    link = runtime._build_ton_usdt_wallet_link(
        destination_address="UQTESTADDRESS",
        expected_amount_usdt=Decimal("1.200100"),
        text="QPI deposit D91",
    )

    assert link.startswith("ton://transfer/UQTESTADDRESS?")
    assert "jetton=EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs" in link
    assert "amount=1200100" in link
    assert "text=QPI+deposit+D91" in link


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
    runtime = _build_runtime()

    assert runtime._format_copyable_code("UQ_TEST") == "<code>UQ_TEST</code>"


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

    assert "🆘 Поддержка" not in set(_flatten_labels(runtime._seller_menu_markup()))
    assert "🆘 Поддержка" not in set(_flatten_labels(runtime._buyer_menu_markup()))


def test_seller_dashboard_keeps_support_but_buyer_dashboard_hides_it() -> None:
    runtime = _build_runtime()

    assert "🆘 Поддержка" in set(_flatten_labels(runtime._seller_menu_markup()))
    assert "🆘 Поддержка" not in set(_flatten_labels(runtime._buyer_menu_markup()))


def test_seller_listing_detail_markup_hides_edit_button_when_activation_is_blocked() -> None:
    runtime = _build_runtime()

    markup = runtime._seller_listing_detail_markup(
        listing_id=21,
        status="draft",
        list_page=1,
        can_activate=False,
    )
    labels = _flatten_labels(markup)

    assert "✏️ Редактировать" not in labels
    assert "⛔ Недостаточно средств" in labels
