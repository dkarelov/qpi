from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class WbPingResult:
    valid: bool
    status_code: int | None
    message: str | None = None


class WbPingClient:
    """Minimal WB ping validator with in-process rate limiting."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 10,
        max_requests: int = 3,
        window_seconds: int = 30,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._request_timestamps: deque[float] = deque()

    async def validate_token(self, token: str) -> WbPingResult:
        if not token.strip():
            return WbPingResult(valid=False, status_code=None, message="empty token")

        await self._acquire_slot()
        return await asyncio.to_thread(self._validate_token_sync, token.strip())

    async def _acquire_slot(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._request_timestamps and (
                    now - self._request_timestamps[0] >= self._window_seconds
                ):
                    self._request_timestamps.popleft()
                if len(self._request_timestamps) < self._max_requests:
                    self._request_timestamps.append(now)
                    return
                wait_for = self._window_seconds - (now - self._request_timestamps[0])
            await asyncio.sleep(max(wait_for, 0.05))

    def _validate_token_sync(self, token: str) -> WbPingResult:
        statistics_result = _request_ping(
            url="https://statistics-api.wildberries.ru/ping",
            token=token,
            timeout_seconds=self._timeout_seconds,
        )
        if not statistics_result.valid:
            return statistics_result

        content_result = _request_ping(
            url="https://content-api.wildberries.ru/ping",
            token=token,
            timeout_seconds=self._timeout_seconds,
        )
        if not content_result.valid:
            message = content_result.message or "content ping failed"
            return WbPingResult(
                valid=False,
                status_code=content_result.status_code,
                message=f"content access missing: {message[:450]}",
            )

        return WbPingResult(valid=True, status_code=statistics_result.status_code, message="ok")


def _request_ping(*, url: str, token: str, timeout_seconds: int) -> WbPingResult:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": token,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
            status = response.getcode()
            if 200 <= status < 300:
                return WbPingResult(valid=True, status_code=status, message="ok")
            return WbPingResult(valid=False, status_code=status, message=payload[:500])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = _extract_message(body) or body or exc.reason
        return WbPingResult(valid=False, status_code=exc.code, message=message[:500])
    except urllib.error.URLError as exc:
        return WbPingResult(valid=False, status_code=None, message=str(exc.reason))
    except TimeoutError:
        return WbPingResult(valid=False, status_code=None, message="timeout")


def _extract_message(payload: str) -> str | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        for key in ("message", "error", "title"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
    return None
