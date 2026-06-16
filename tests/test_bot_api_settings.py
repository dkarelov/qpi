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
    "proxy_url",
    [
        "http://user:pass@proxy.example:8000",
        "https://proxy.example:8000",
    ],
)
def test_bot_api_settings_accept_http_proxy_urls(proxy_url: str) -> None:
    settings = BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URL=proxy_url))

    assert settings.telegram_api_proxy_url == proxy_url


@pytest.mark.parametrize("proxy_url", ["", "   "])
def test_bot_api_settings_normalizes_blank_proxy_url(proxy_url: str) -> None:
    settings = BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URL=proxy_url))

    assert settings.telegram_api_proxy_url is None


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
    with pytest.raises(ValidationError, match="TELEGRAM_API_PROXY_URL must be an HTTP\\(S\\) proxy URL"):
        BotApiSettings.model_validate(_base_settings(TELEGRAM_API_PROXY_URL=proxy_url))
