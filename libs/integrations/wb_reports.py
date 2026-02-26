from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class WbReportApiError(Exception):
    status_code: int | None
    message: str

    def __str__(self) -> str:
        prefix = str(self.status_code) if self.status_code is not None else "network"
        return f"{prefix}: {self.message}"


class WbReportClient:
    """Minimal async client for WB reportDetailByPeriod endpoint."""

    def __init__(
        self,
        *,
        endpoint: str = "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod",
        timeout_seconds: int = 60,
    ) -> None:
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    async def fetch_report_detail_page(
        self,
        *,
        token: str,
        date_from: date,
        date_to: date,
        rrd_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._fetch_report_detail_page_sync,
            token.strip(),
            date_from,
            date_to,
            rrd_id,
            limit,
        )

    def _fetch_report_detail_page_sync(
        self,
        token: str,
        date_from: date,
        date_to: date,
        rrd_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": str(limit),
                "rrdid": str(rrd_id),
            }
        )
        request = urllib.request.Request(
            f"{self._endpoint}?{query}",
            method="GET",
            headers={
                "Authorization": token,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
                if not 200 <= status < 300:
                    message = _extract_message(payload) or payload or "unexpected response status"
                    raise WbReportApiError(status_code=status, message=message[:1000])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = _extract_message(body) or body or str(exc.reason)
            raise WbReportApiError(status_code=exc.code, message=message[:1000]) from exc
        except urllib.error.URLError as exc:
            raise WbReportApiError(status_code=None, message=str(exc.reason)) from exc
        except TimeoutError as exc:
            raise WbReportApiError(status_code=None, message="timeout") from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WbReportApiError(status_code=200, message="invalid JSON response") from exc

        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            rows = data.get("data")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]

        raise WbReportApiError(status_code=200, message="unexpected response body")


def _extract_message(payload: str) -> str | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        for key in ("message", "error", "title", "detail"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value

    return None
