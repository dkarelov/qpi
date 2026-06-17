from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from libs.logging.setup import EventLogger

_DEFAULT_METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
_DEFAULT_MONITORING_WRITE_URL = "https://monitoring.api.cloud.yandex.net/monitoring/v2/data/write"
_DEFAULT_MONITORING_TIMEOUT_SECONDS = 5.0
_TOKEN_REFRESH_MARGIN_SECONDS = 60


class _UrlOpenResponse(Protocol):
    def __enter__(self) -> _UrlOpenResponse: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def read(self) -> bytes: ...


UrlOpen = Callable[..., _UrlOpenResponse]


@dataclass(frozen=True)
class _CachedIamToken:
    value: str
    expires_at: float


class MetadataIamTokenProvider:
    def __init__(
        self,
        *,
        metadata_token_url: str = _DEFAULT_METADATA_TOKEN_URL,
        timeout_seconds: float = 2.0,
        urlopen: UrlOpen = urllib.request.urlopen,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._metadata_token_url = metadata_token_url
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen
        self._now = now
        self._cached_token: _CachedIamToken | None = None
        self._lock = Lock()

    def get_token(self) -> str:
        now = self._now()
        if self._cached_token and self._cached_token.expires_at > now:
            return self._cached_token.value

        with self._lock:
            now = self._now()
            if self._cached_token and self._cached_token.expires_at > now:
                return self._cached_token.value

            request = urllib.request.Request(
                self._metadata_token_url,
                headers={"Metadata-Flavor": "Google"},
            )
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))

            access_token = str(payload.get("access_token") or "").strip()
            if not access_token:
                raise RuntimeError("metadata IAM token response did not include access_token")

            expires_in = int(payload.get("expires_in") or _TOKEN_REFRESH_MARGIN_SECONDS)
            expires_at = now + max(0, expires_in - _TOKEN_REFRESH_MARGIN_SECONDS)
            self._cached_token = _CachedIamToken(value=access_token, expires_at=expires_at)
            return access_token


class YandexMonitoringMetricClient:
    def __init__(
        self,
        *,
        folder_id: str | None,
        token_provider: MetadataIamTokenProvider | None = None,
        endpoint_url: str = _DEFAULT_MONITORING_WRITE_URL,
        timeout_seconds: float = _DEFAULT_MONITORING_TIMEOUT_SECONDS,
        urlopen: UrlOpen = urllib.request.urlopen,
    ) -> None:
        self._folder_id = (folder_id or "").strip()
        self._token_provider = token_provider or MetadataIamTokenProvider(timeout_seconds=timeout_seconds)
        self._endpoint_url = endpoint_url
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen

    def write_metric(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        if not self._folder_id:
            return

        query = urllib.parse.urlencode({"folderId": self._folder_id, "service": "custom"})
        url = f"{self._endpoint_url}?{query}"
        body = json.dumps(
            {
                "metrics": [
                    {
                        "name": name,
                        "labels": labels,
                        "type": "DGAUGE",
                        "value": value,
                    }
                ]
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token_provider.get_token()}",
                "Content-Type": "application/json",
            },
        )
        with self._urlopen(request, timeout=self._timeout_seconds) as response:
            response.read()


class YandexMonitoringMetricRecorder:
    def __init__(
        self,
        *,
        client: YandexMonitoringMetricClient,
        logger: EventLogger | None = None,
    ) -> None:
        self._client = client
        self._logger = logger

    def record(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_metric_safely(name, labels, value)
            return

        task = loop.create_task(asyncio.to_thread(self._write_metric_safely, name, labels, value))
        task.add_done_callback(self._log_unexpected_task_error)

    def _write_metric_safely(self, name: str, labels: dict[str, str], value: float) -> None:
        try:
            self._client.write_metric(name, labels, value)
        except Exception as exc:
            if self._logger:
                self._logger.warning(
                    "yandex_monitoring_metric_write_failed",
                    metric_name=name,
                    error_type=type(exc).__name__,
                )

    def _log_unexpected_task_error(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc and self._logger:
            self._logger.warning(
                "yandex_monitoring_metric_task_failed",
                error_type=type(exc).__name__,
            )
