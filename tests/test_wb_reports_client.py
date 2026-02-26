from __future__ import annotations

import urllib.parse
from datetime import date

import pytest

from libs.integrations.wb_reports import WbReportApiError, WbReportClient


class _StubResponse:
    def __init__(self, *, status: int, body: str) -> None:
        self._status = status
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self._status


@pytest.mark.asyncio
async def test_wb_report_client_treats_204_as_empty_page(monkeypatch) -> None:
    client = WbReportClient()

    def _fake_urlopen(request, timeout):
        return _StubResponse(status=204, body="")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    rows = await client.fetch_report_detail_page(
        token="token",
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 2),
        rrd_id=0,
        limit=100,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_wb_report_client_treats_empty_payload_as_empty_page(monkeypatch) -> None:
    client = WbReportClient()

    def _fake_urlopen(request, timeout):
        return _StubResponse(status=200, body="   ")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    rows = await client.fetch_report_detail_page(
        token="token",
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 2),
        rrd_id=0,
        limit=100,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_wb_report_client_raises_for_invalid_json_payload(monkeypatch) -> None:
    client = WbReportClient()

    def _fake_urlopen(request, timeout):
        return _StubResponse(status=200, body="not-json")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    with pytest.raises(WbReportApiError) as exc_info:
        await client.fetch_report_detail_page(
            token="token",
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 2),
            rrd_id=0,
            limit=100,
        )

    assert exc_info.value.status_code == 200
    assert "invalid JSON" in exc_info.value.message


@pytest.mark.asyncio
async def test_wb_report_client_sends_daily_period_param(monkeypatch) -> None:
    client = WbReportClient(endpoint="https://example.test/report")
    captured_url: str | None = None

    def _fake_urlopen(request, timeout):
        nonlocal captured_url
        captured_url = request.full_url
        return _StubResponse(status=200, body="[]")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    rows = await client.fetch_report_detail_page(
        token="token",
        date_from=date(2026, 2, 23),
        date_to=date(2026, 2, 25),
        rrd_id=0,
        limit=100,
    )

    assert rows == []
    assert captured_url is not None
    parsed = urllib.parse.urlparse(captured_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["period"] == ["daily"]
    assert query["dateFrom"] == ["2026-02-23"]
    assert query["dateTo"] == ["2026-02-25"]
