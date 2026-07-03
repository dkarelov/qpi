from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from services.bot_api.presentation import (
    button_label_with_count,
    buyer_listing_detail_html,
    entity_block_heading_with_ref,
    format_buyer_cashback_with_percent,
    format_datetime_msk,
    format_price_optional_rub,
    format_usdt_value,
    numbered_page_buttons,
    screen_text,
    withdraw_status_badge,
)


def test_screen_text_decorates_titles_from_shared_union_table() -> None:
    assert screen_text(title="Кабинет продавца") == "<b>🧑‍💼 Кабинет продавца</b>"
    assert screen_text(title="Кабинет покупателя") == "<b>🛍️ Кабинет покупателя</b>"
    assert screen_text(title="Токен WB API") == "<b>🏪 Токен WB API</b>"
    assert screen_text(title="Проверьте объявление") == "<b>📦 Проверьте объявление</b>"
    assert screen_text(title="Счет на пополнение") == "<b>💰 Счет на пополнение</b>"
    assert screen_text(title="Отмена вывода") == "<b>💳 Отмена вывода</b>"


def test_screen_text_keeps_ref_suffix_outside_bold_title_and_separates_blocks() -> None:
    text = screen_text(
        title="Магазин",
        title_suffix_html=" · <code>S4</code>",
        cta="Проверьте данные.",
        lines=["Первый блок", "Второй блок"],
        note="Подсказка.",
        separate_blocks=True,
    )

    assert text == (
        "<b>🏪 Магазин</b> · <code>S4</code>\n\n"
        "<i>Проверьте данные.</i>\n\n"
        "Первый блок\n\n"
        "Второй блок\n\n"
        "<i>Подсказка.</i>"
    )


def test_badges_money_and_moscow_datetime_format_at_presentation_boundary() -> None:
    assert withdraw_status_badge("withdraw_pending_admin") == "🟡 На проверке"
    assert withdraw_status_badge("rejected") == "🔴 Отклонено"
    assert entity_block_heading_with_ref(label="Вывод", ref="W7") == "<b>Вывод</b> · <code>W7</code>"
    assert format_usdt_value(Decimal("1.234567"), precise=True) == "1.234567"
    assert (
        format_buyer_cashback_with_percent(
            reward_usdt=Decimal("1.000000"),
            reference_price_rub=1200,
            display_rub_per_usdt=Decimal("100"),
        )
        == "~100 ₽ (~8%)"
    )
    assert format_price_optional_rub(None) == "—"
    assert format_datetime_msk(datetime(2026, 3, 2, 14, 0, tzinfo=UTC)) == "02.03.2026 17:00 МСК"


def test_buyer_listing_detail_html_renders_announcement_blocks_without_zero_size() -> None:
    listing = SimpleNamespace(
        listing_id=21,
        display_title="Белорусская тушенка",
        search_phrase="тушенка белорусская",
        wb_subject_name="Консервы",
        reference_price_rub=490,
        reward_usdt=Decimal("1.250000"),
        wb_tech_sizes=["0"],
        wb_description="Говядина",
        wb_characteristics=[{"name": "Вес", "value": "325 г"}],
    )

    text = buyer_listing_detail_html(listing=listing, display_rub_per_usdt=Decimal("100"))

    assert "<b>📦 Белорусская тушенка</b> · <code>L21</code>" in text
    assert "<b>Предмет:</b> Консервы" in text
    assert "<b>Цена:</b> 490 ₽" in text
    assert "<b>Кэшбэк:</b> ~125 ₽ (~26%)" in text
    assert "<b>Размеры:</b>" not in text
    assert "<b>Описание</b>\n<blockquote expandable>Говядина</blockquote>" in text
    assert "<b>Характеристики</b>\n<blockquote expandable>Вес: 325 г</blockquote>" in text


def test_numbered_page_helpers_return_stable_button_specs() -> None:
    buttons = numbered_page_buttons(
        flow="buyer",
        open_action="listing_detail",
        page_action="shop_page",
        item_ids=[11, 12, 13, 14, 15, 16],
        start_number=6,
        page=2,
        total_pages=3,
    )

    assert button_label_with_count("Покупки", 2) == "Покупки · 2"
    assert [button.text for button in buttons[0]] == ["6", "7", "8", "9", "10"]
    assert [button.text for button in buttons[1]] == ["11"]
    assert [(button.text, button.action, button.entity_id) for button in buttons[2]] == [
        ("⬅️", "shop_page", "1"),
        ("➡️", "shop_page", "3"),
    ]
