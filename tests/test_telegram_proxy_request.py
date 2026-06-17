from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.request import BaseRequest, RequestData

from services.bot_api.telegram_proxy_request import (
    REQUEST_ATTEMPT_METRIC,
    REQUEST_EXHAUSTED_METRIC,
    AlternatingTelegramProxyRequest,
)

_OK_PAYLOAD = b'{"ok":true,"result":true}'
_BAD_REQUEST_PAYLOAD = b'{"ok":false,"description":"bad request"}'
_RETRY_AFTER_PAYLOAD = b'{"ok":false,"description":"too many requests","parameters":{"retry_after":30}}'
_SERVER_ERROR_PAYLOAD = b'{"ok":false,"description":"bad gateway"}'


class _FakeRequest(BaseRequest):
    def __init__(self, responses: Sequence[tuple[int, bytes] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        self.calls.append(method)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeRequestFactory:
    def __init__(self, responses_by_proxy: dict[str | None, Sequence[tuple[int, bytes] | Exception]]) -> None:
        self.requests: dict[str | None, _FakeRequest] = {}
        self._responses_by_proxy = responses_by_proxy

    def __call__(self, proxy_url: str | None) -> _FakeRequest:
        request = _FakeRequest(self._responses_by_proxy[proxy_url])
        self.requests[proxy_url] = request
        return request


class _MetricRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, str], float]] = []

    def record(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        self.records.append((name, labels, value))


def _build_request(
    factory: _FakeRequestFactory,
    recorder: _MetricRecorder,
) -> AlternatingTelegramProxyRequest:
    return AlternatingTelegramProxyRequest(
        proxy_urls=(
            "http://user:secret@proxy-one.example:8000",
            "http://proxy-two.example:8000",
        ),
        metric_recorder=recorder,
        request_factory=factory,
    )


@pytest.mark.asyncio
async def test_proxy_request_uses_first_proxy_on_success_and_records_sanitized_metric_labels() -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [(200, _OK_PAYLOAD)],
            "http://proxy-two.example:8000": [],
        }
    )
    request = _build_request(factory, recorder)

    result = await request.post("https://api.telegram.org/bot123:secret-token/sendMessage")

    assert result is True
    assert len(factory.requests["http://user:secret@proxy-one.example:8000"].calls) == 1
    assert len(factory.requests["http://proxy-two.example:8000"].calls) == 0
    assert recorder.records == [
        (
            REQUEST_ATTEMPT_METRIC,
            {
                "attempt_number": "1",
                "outcome": "success",
                "proxy_host": "proxy-one.example:8000",
                "proxy_index": "1",
                "status_code": "200",
                "telegram_method": "sendMessage",
            },
            1.0,
        )
    ]
    assert "secret" not in repr(recorder.records)
    assert "123:secret-token" not in repr(recorder.records)


@pytest.mark.asyncio
async def test_proxy_request_alternates_retry_order_for_transport_failures() -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [NetworkError("p1 down"), (200, _OK_PAYLOAD)],
            "http://proxy-two.example:8000": [NetworkError("p2 down")],
        }
    )
    request = _build_request(factory, recorder)

    result = await request.post("https://api.telegram.org/bot123/getMe")

    assert result is True
    assert len(factory.requests["http://user:secret@proxy-one.example:8000"].calls) == 2
    assert len(factory.requests["http://proxy-two.example:8000"].calls) == 1
    assert [record[1]["proxy_index"] for record in recorder.records] == ["1", "2", "1"]
    assert [record[1]["outcome"] for record in recorder.records] == ["failure", "failure", "success"]


@pytest.mark.asyncio
async def test_proxy_request_exhausts_after_three_attempts_per_proxy() -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [
                NetworkError("p1 down"),
                NetworkError("p1 down"),
                NetworkError("p1 down"),
            ],
            "http://proxy-two.example:8000": [
                NetworkError("p2 down"),
                NetworkError("p2 down"),
                NetworkError("p2 down"),
            ],
        }
    )
    request = _build_request(factory, recorder)

    with pytest.raises(NetworkError, match="p2 down"):
        await request.post("https://api.telegram.org/bot123/getMe")

    assert len(factory.requests["http://user:secret@proxy-one.example:8000"].calls) == 3
    assert len(factory.requests["http://proxy-two.example:8000"].calls) == 3
    assert [record[0] for record in recorder.records].count(REQUEST_ATTEMPT_METRIC) == 6
    exhausted_records = [record for record in recorder.records if record[0] == REQUEST_EXHAUSTED_METRIC]
    assert len(exhausted_records) == 1
    assert exhausted_records[0][1]["proxy_index"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected_error"),
    [
        (400, _BAD_REQUEST_PAYLOAD, BadRequest),
        (429, _RETRY_AFTER_PAYLOAD, RetryAfter),
    ],
)
async def test_proxy_request_does_not_retry_semantic_errors(
    status_code: int,
    payload: bytes,
    expected_error: type[Exception],
) -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [(status_code, payload)],
            "http://proxy-two.example:8000": [(200, _OK_PAYLOAD)],
        }
    )
    request = _build_request(factory, recorder)

    with pytest.raises(expected_error):
        await request.post("https://api.telegram.org/bot123/sendMessage")

    assert len(factory.requests["http://user:secret@proxy-one.example:8000"].calls) == 1
    assert len(factory.requests["http://proxy-two.example:8000"].calls) == 0
    assert recorder.records[0][1]["outcome"] == "semantic_error"
    assert recorder.records[0][1]["status_code"] == str(status_code)


@pytest.mark.asyncio
async def test_proxy_request_retries_http_5xx() -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [(502, _SERVER_ERROR_PAYLOAD)],
            "http://proxy-two.example:8000": [(200, _OK_PAYLOAD)],
        }
    )
    request = _build_request(factory, recorder)

    result = await request.post("https://api.telegram.org/bot123/sendMessage")

    assert result is True
    assert [record[1]["proxy_index"] for record in recorder.records] == ["1", "2"]
    assert [record[1]["outcome"] for record in recorder.records] == ["failure", "success"]


@pytest.mark.asyncio
async def test_proxy_request_records_exhaustion_for_repeated_http_5xx() -> None:
    recorder = _MetricRecorder()
    factory = _FakeRequestFactory(
        {
            "http://user:secret@proxy-one.example:8000": [
                (502, _SERVER_ERROR_PAYLOAD),
                (502, _SERVER_ERROR_PAYLOAD),
                (502, _SERVER_ERROR_PAYLOAD),
            ],
            "http://proxy-two.example:8000": [
                (502, _SERVER_ERROR_PAYLOAD),
                (502, _SERVER_ERROR_PAYLOAD),
                (502, _SERVER_ERROR_PAYLOAD),
            ],
        }
    )
    request = _build_request(factory, recorder)

    with pytest.raises(NetworkError, match="bad gateway"):
        await request.post("https://api.telegram.org/bot123/getMe")

    assert [record[0] for record in recorder.records].count(REQUEST_ATTEMPT_METRIC) == 6
    exhausted_records = [record for record in recorder.records if record[0] == REQUEST_EXHAUSTED_METRIC]
    assert len(exhausted_records) == 1
    assert exhausted_records[0][1]["status_code"] == "502"
