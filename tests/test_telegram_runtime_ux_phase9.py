from __future__ import annotations

from libs.config.settings import BotApiSettings
from services.bot_api.telegram_runtime import TelegramWebhookRuntime


def _build_runtime() -> TelegramWebhookRuntime:
    settings = BotApiSettings.model_validate(
        {
            "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
            "TOKEN_CIPHER_KEY": "test-key",
            "ADMIN_TELEGRAM_IDS": [1],
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
