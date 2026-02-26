"""External service integrations."""

from libs.integrations.wb import WbPingClient, WbPingResult
from libs.integrations.wb_reports import WbReportApiError, WbReportClient

__all__ = ["WbPingClient", "WbPingResult", "WbReportClient", "WbReportApiError"]
