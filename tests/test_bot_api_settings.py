from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.config.settings import BotApiSettings


def _base_settings(**overrides: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "DATABASE_URL": "postgresql://user:pass@127.0.0.1:5432/qpi_test",
        "TOKEN_CIPHER_KEY": "test-key",
    }
    settings.update(overrides)
    return settings


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (
            "http://user:pass@proxy-one.example:8000,https://proxy-two.example:8000",
            ("http://user:pass@proxy-one.example:8000", "https://proxy-two.example:8000"),
        ),
        (
            "http://proxy-one.example:8000\nhttps://proxy-two.example:8000",
            ("http://proxy-one.example:8000", "https://proxy-two.example:8000"),
        ),
    ],
)
def test_bot_api_settings_accepts_ordered_http_proxy_urls(raw_value: str, expected: tuple[str, ...]) -> None:
    settings = BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URLS=raw_value))

    assert settings.telegram_api_proxy_urls == expected


@pytest.mark.parametrize("raw_value", ["", "   "])
def test_bot_api_settings_normalizes_blank_proxy_url_list(raw_value: str) -> None:
    settings = BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URLS=raw_value))

    assert settings.telegram_api_proxy_urls == ()


@pytest.mark.parametrize(
    "proxy_url",
    [
        "socks5://proxy.example:1080",
        "ftp://proxy.example:21",
        "http:///missing-host",
        "proxy.example:8000",
    ],
)
def test_bot_api_settings_rejects_unsupported_proxy_urls(proxy_url: str) -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_API_PROXY_URLS must contain only HTTP\\(S\\) proxy URLs"):
        BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URLS=proxy_url))


def test_bot_api_settings_rejects_legacy_single_proxy_url() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_API_PROXY_URL is no longer supported"):
        BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URL="http://proxy.example:8000"))


def test_bot_api_settings_requires_two_proxy_urls_in_prod() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_API_PROXY_URLS must contain at least two proxy URLs in prod"):
        BotApiSettings.model_validate(
            _base_settings(
                APP_ENV="prod",
                YC_FOLDER_ID="b1folder",
                TELEGRAM_API_PROXY_URLS="http://proxy.example:8000",
            )
        )


def test_bot_api_settings_requires_folder_id_in_prod_for_proxy_metrics() -> None:
    with pytest.raises(ValidationError, match="YC_FOLDER_ID is required in prod"):
        BotApiSettings.model_validate(
            _base_settings(
                APP_ENV="prod",
                TELEGRAM_API_PROXY_URLS="http://proxy-one.example:8000,http://proxy-two.example:8000",
            )
        )
