from __future__ import annotations

import asyncio
import json
from typing import Any

from libs.integrations.yandex_monitoring import (
    MetadataIamTokenProvider,
    YandexMonitoringMetricClient,
    YandexMonitoringMetricRecorder,
)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _StaticTokenProvider:
    def get_token(self) -> str:
        return "iam-token"


def test_yandex_monitoring_client_writes_custom_metric_payload() -> None:
    requests: list[object] = []

    def urlopen(request: object, timeout: float) -> _Response:
        requests.append(request)
        assert timeout == 2.0
        return _Response({"metrics_written": "1"})

    client = YandexMonitoringMetricClient(
        folder_id="b1folder",
        token_provider=_StaticTokenProvider(),  # type: ignore[arg-type]
        endpoint_url="https://monitoring.example/write",
        urlopen=urlopen,  # type: ignore[arg-type]
    )

    client.write_metric("qpi.telegram.proxy.request_attempt", {"proxy_index": "1", "outcome": "success"})

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://monitoring.example/write?folderId=b1folder&service=custom"
    assert request.headers["Authorization"] == "Bearer iam-token"
    assert request.headers["Content-type"] == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {
        "metrics": [
            {
                "labels": {"outcome": "success", "proxy_index": "1"},
                "name": "qpi.telegram.proxy.request_attempt",
                "type": "DGAUGE",
                "value": 1.0,
            }
        ]
    }


def test_metadata_iam_token_provider_caches_token_until_refresh_margin() -> None:
    calls = 0
    now = 1_000.0

    def current_time() -> float:
        return now

    def urlopen(request: object, timeout: float) -> _Response:
        nonlocal calls
        calls += 1
        assert request.headers["Metadata-flavor"] == "Google"
        assert timeout == 2.0
        return _Response({"access_token": f"token-{calls}", "expires_in": 120})

    provider = MetadataIamTokenProvider(urlopen=urlopen, now=current_time)  # type: ignore[arg-type]

    assert provider.get_token() == "token-1"
    assert provider.get_token() == "token-1"
    assert calls == 1

    now = 1_061.0

    assert provider.get_token() == "token-2"
    assert calls == 2


async def test_metric_recorder_does_not_raise_on_metric_send_failure() -> None:
    class FailingClient:
        def write_metric(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
            raise RuntimeError("monitoring unavailable")

    class Logger:
        def __init__(self) -> None:
            self.warnings: list[dict[str, str]] = []

        def warning(self, event: str, **fields: str) -> None:
            self.warnings.append({"event": event, **fields})

    logger = Logger()
    recorder = YandexMonitoringMetricRecorder(client=FailingClient(), logger=logger)  # type: ignore[arg-type]

    recorder.record("qpi.telegram.proxy.request_attempt", {"proxy_index": "1"})
    await asyncio.sleep(0.05)

    assert logger.warnings == [
        {
            "error_type": "RuntimeError",
            "event": "yandex_monitoring_metric_write_failed",
            "metric_name": "qpi.telegram.proxy.request_attempt",
        }
    ]
