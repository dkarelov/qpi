from __future__ import annotations

from decimal import Decimal

from libs.config.settings import BotApiSettings
from services.bot_api.callback_data import build_callback
from services.bot_api.telegram_runtime import TelegramWebhookRuntime


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
    assert "📦 Листинги" in labels_set
    assert "💰 Баланс" in labels_set
    assert "➕ Создать магазин" not in labels_set
    assert "➕ Создать листинг" not in labels_set
    assert "➕ Пополнить" not in labels_set


def test_buyer_menu_is_dashboard_sections() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._buyer_menu_markup())
    labels_set = set(labels)

    assert "🏪 Магазины" in labels_set
    assert "📋 Задания" in labels_set
    assert "💳 Баланс и вывод" in labels_set


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


def test_seller_shop_detail_menu_is_structured() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_shop_detail_markup(shop_id=1, token_is_valid=False))
    labels_set = set(labels)

    assert "❌ Токен WB API" in labels_set
    assert "✏️ Переименовать" in labels_set
    assert "🗑 Удалить" in labels_set
    assert "↩️ К списку магазинов" in labels_set
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


def test_seller_balance_menu_uses_transactions_label() -> None:
    runtime = _build_runtime()

    labels = _flatten_labels(runtime._seller_balance_menu_markup())
    labels_set = set(labels)

    assert "🧾 Транзакции" in labels_set
    assert "🧾 Мои пополнения / Проверить" not in labels_set


def test_money_formatter_uses_usdt_with_approx_rub() -> None:
    runtime = _build_runtime()

    assert runtime._format_usdt_with_rub(Decimal("1.24")) == "$1.2 (~124 ₽)"
    assert runtime._format_usdt_with_rub(Decimal("1.25")) == "$1.3 (~125 ₽)"
    assert runtime._format_usdt(Decimal("1.234567"), precise=True) == "$1.234567"


def test_token_instruction_contains_required_sections() -> None:
    runtime = _build_runtime()

    text = runtime._shop_token_instruction_text(shop_title="Мой магазин")
    assert "Отправьте сообщением токен WB API." in text
    assert "Зачем нужен токен?" in text
    assert "Где найти токен?" in text
    assert "Безопасно ли это?" in text
