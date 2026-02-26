from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

_CONFIGURED = False
_LOG_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("qpi_log_context", default=None)


class EventLogger:
    """Compatibility wrapper: supports logger.info("event", key=value)."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _compose_extra(self, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        extra = dict(_LOG_CONTEXT.get() or {})
        if fields:
            extra.update(fields)
        return extra

    def _render_value(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            text = value.replace("\n", "\\n").strip()
            if not text:
                return '""'
            if any(char.isspace() for char in text):
                return f'"{text}"'
            return text
        if isinstance(value, (int, float, bool)) or value is None:
            return str(value)
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except TypeError:
            return repr(value)

    def _format_event(self, event: object, fields: dict[str, Any]) -> str:
        message = str(event)
        if not fields:
            return message
        rendered_fields = " ".join(
            f"{key}={self._render_value(fields[key])}" for key in sorted(fields)
        )
        return f"{message} {rendered_fields}"

    def _log(self, level: int, event: object, **fields: Any) -> None:
        self._logger.log(
            level,
            self._format_event(event, fields),
            extra=self._compose_extra(fields),
        )

    def debug(self, event: object, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: object, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: object, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: object, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: object, **fields: Any) -> None:
        self._logger.error(
            self._format_event(event, fields),
            exc_info=True,
            extra=self._compose_extra(fields),
        )


def configure_logging(
    service_name: str,
    log_level: str = "INFO",
    *,
    request_id: str | None = None,
) -> None:
    """Configure JSON structured logging once per process."""

    global _CONFIGURED

    level = getattr(logging, log_level.upper(), logging.INFO)

    if not _CONFIGURED:
        cf_env = request_id is not None
        try:
            from yc_json_logger import setup_logging as yc_setup_logging
        except ImportError:
            # Local fallback for environments where private dependency isn't installed yet.
            logging.basicConfig(format="%(levelname)s %(message)s", level=level, force=True)
        else:
            try:
                yc_setup_logging(level=level, request_id="", cf_env=cf_env)
            except Exception:
                # Defensive fallback if runtime logger handlers are not in expected state.
                logging.basicConfig(format="%(levelname)s %(message)s", level=level, force=True)
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(level)


    context: dict[str, Any] = {"service": service_name}
    if request_id:
        context["request_id"] = request_id
    _LOG_CONTEXT.set(context)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))
