from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from telegram.error import NetworkError
from telegram.request import BaseRequest, HTTPXRequest, RequestData

from libs.integrations.yandex_monitoring import YandexMonitoringMetricClient, YandexMonitoringMetricRecorder
from libs.logging.setup import EventLogger

REQUEST_ATTEMPT_METRIC = "qpi.telegram.proxy.request_attempt"
REQUEST_EXHAUSTED_METRIC = "qpi.telegram.proxy.request_exhausted"
_MAX_ATTEMPTS_PER_PROXY = 3


@dataclass(frozen=True)
class TelegramProxyEndpoint:
    index: int
    url: str | None
    label: str

    @property
    def index_label(self) -> str:
        return "direct" if self.url is None else str(self.index)


class TelegramProxyMetricRecorder(Protocol):
    def record(self, name: str, labels: dict[str, str], value: float = 1.0) -> None: ...


RequestFactory = Callable[[str | None], BaseRequest]


def build_telegram_proxy_request(
    proxy_urls: Sequence[str],
    *,
    folder_id: str | None,
    logger: EventLogger | None = None,
    request_factory: RequestFactory | None = None,
) -> AlternatingTelegramProxyRequest:
    metric_recorder: TelegramProxyMetricRecorder | None = None
    if folder_id:
        metric_recorder = YandexMonitoringMetricRecorder(
            client=YandexMonitoringMetricClient(folder_id=folder_id),
            logger=logger,
        )
    return AlternatingTelegramProxyRequest(
        proxy_urls=proxy_urls,
        metric_recorder=metric_recorder,
        logger=logger,
        request_factory=request_factory,
    )


class AlternatingTelegramProxyRequest(BaseRequest):
    def __init__(
        self,
        *,
        proxy_urls: Sequence[str],
        metric_recorder: TelegramProxyMetricRecorder | None = None,
        logger: EventLogger | None = None,
        request_factory: RequestFactory | None = None,
        max_attempts_per_proxy: int = _MAX_ATTEMPTS_PER_PROXY,
    ) -> None:
        if max_attempts_per_proxy < 1:
            raise ValueError("max_attempts_per_proxy must be >= 1")
        self._endpoints = tuple(_build_proxy_endpoint(index, url) for index, url in enumerate(proxy_urls, start=1))
        if not self._endpoints:
            self._endpoints = (TelegramProxyEndpoint(index=0, url=None, label="direct"),)
        self._metric_recorder = metric_recorder
        self._logger = logger
        self._max_attempts_per_proxy = max_attempts_per_proxy
        factory = request_factory or (lambda proxy_url: HTTPXRequest(proxy=proxy_url))
        self._requests = tuple(factory(endpoint.url) for endpoint in self._endpoints)
        self._attempt_plan = self._build_attempt_plan()

    async def initialize(self) -> None:
        for request in self._requests:
            await request.initialize()

    async def shutdown(self) -> None:
        for request in reversed(self._requests):
            await request.shutdown()

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
        telegram_method = _telegram_method_from_url(url)
        last_exception: NetworkError | None = None
        last_5xx_response: tuple[int, bytes, TelegramProxyEndpoint] | None = None

        for attempt_number, endpoint_index in enumerate(self._attempt_plan, start=1):
            endpoint = self._endpoints[endpoint_index]
            request = self._requests[endpoint_index]
            try:
                code, payload = await request.do_request(
                    url=url,
                    method=method,
                    request_data=request_data,
                    read_timeout=read_timeout,
                    write_timeout=write_timeout,
                    connect_timeout=connect_timeout,
                    pool_timeout=pool_timeout,
                )
            except NetworkError as exc:
                last_exception = exc
                self._record_attempt(
                    endpoint=endpoint,
                    telegram_method=telegram_method,
                    outcome="failure",
                    attempt_number=attempt_number,
                    error_type=type(exc).__name__,
                )
                if attempt_number < len(self._attempt_plan):
                    continue
                self._record_exhausted(
                    endpoint=endpoint,
                    telegram_method=telegram_method,
                    error_type=type(exc).__name__,
                )
                raise

            if 500 <= code <= 599:
                last_5xx_response = (code, payload, endpoint)
                self._record_attempt(
                    endpoint=endpoint,
                    telegram_method=telegram_method,
                    outcome="failure",
                    attempt_number=attempt_number,
                    status_code=code,
                )
                if attempt_number < len(self._attempt_plan):
                    continue
                self._record_exhausted(
                    endpoint=endpoint,
                    telegram_method=telegram_method,
                    status_code=code,
                )
                return code, payload

            self._record_attempt(
                endpoint=endpoint,
                telegram_method=telegram_method,
                outcome="success" if 200 <= code <= 299 else "semantic_error",
                attempt_number=attempt_number,
                status_code=code,
            )
            return code, payload

        if last_exception:
            raise last_exception
        if last_5xx_response:
            code, payload, endpoint = last_5xx_response
            self._record_exhausted(endpoint=endpoint, telegram_method=telegram_method, status_code=code)
            return code, payload
        raise NetworkError("Telegram request exhausted without a response")

    def _build_attempt_plan(self) -> tuple[int, ...]:
        if len(self._endpoints) == 1 and self._endpoints[0].url is None:
            return (0,)
        return tuple(
            endpoint_index
            for _round in range(self._max_attempts_per_proxy)
            for endpoint_index in range(len(self._endpoints))
        )

    def _record_attempt(
        self,
        *,
        endpoint: TelegramProxyEndpoint,
        telegram_method: str,
        outcome: str,
        attempt_number: int,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        labels = _base_metric_labels(endpoint=endpoint, telegram_method=telegram_method)
        labels["outcome"] = outcome
        labels["attempt_number"] = str(attempt_number)
        if status_code is not None:
            labels["status_code"] = str(status_code)
        if error_type:
            labels["error_type"] = error_type
        self._record_metric(REQUEST_ATTEMPT_METRIC, labels)

    def _record_exhausted(
        self,
        *,
        endpoint: TelegramProxyEndpoint,
        telegram_method: str,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        labels = _base_metric_labels(endpoint=endpoint, telegram_method=telegram_method)
        if status_code is not None:
            labels["status_code"] = str(status_code)
        if error_type:
            labels["error_type"] = error_type
        self._record_metric(REQUEST_EXHAUSTED_METRIC, labels)

    def _record_metric(self, name: str, labels: dict[str, str]) -> None:
        if not self._metric_recorder:
            return
        try:
            self._metric_recorder.record(name, labels, 1.0)
        except Exception as exc:
            if self._logger:
                self._logger.warning(
                    "telegram_proxy_metric_record_failed",
                    metric_name=name,
                    error_type=type(exc).__name__,
                )


def _build_proxy_endpoint(index: int, url: str) -> TelegramProxyEndpoint:
    parsed = urlparse(url)
    host_label = parsed.hostname or "unknown"
    if parsed.port:
        host_label = f"{host_label}:{parsed.port}"
    return TelegramProxyEndpoint(index=index, url=url, label=host_label)


def _base_metric_labels(*, endpoint: TelegramProxyEndpoint, telegram_method: str) -> dict[str, str]:
    return {
        "proxy_index": endpoint.index_label,
        "proxy_host": endpoint.label,
        "telegram_method": telegram_method,
    }


def _telegram_method_from_url(url: str) -> str:
    parsed = urlparse(url)
    method = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return method or "unknown"
