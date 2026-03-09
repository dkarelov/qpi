from __future__ import annotations

import pytest

from libs.integrations.wb import WbPingClient, WbPingResult


@pytest.mark.asyncio
async def test_wb_ping_client_calls_request_ping_with_keyword_args(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_request_ping(*, url: str, token: str, timeout_seconds: int) -> WbPingResult:
        calls.append(
            {
                "url": url,
                "token": token,
                "timeout_seconds": timeout_seconds,
            }
        )
        return WbPingResult(valid=True, status_code=200, message="ok")

    monkeypatch.setattr("libs.integrations.wb._request_ping", _fake_request_ping)

    client = WbPingClient(timeout_seconds=7)
    result = await client.validate_token("wb-token")

    assert result.valid is True
    assert calls == [
        {
            "url": "https://statistics-api.wildberries.ru/ping",
            "token": "wb-token",
            "timeout_seconds": 7,
        },
        {
            "url": "https://content-api.wildberries.ru/ping",
            "token": "wb-token",
            "timeout_seconds": 7,
        },
    ]
