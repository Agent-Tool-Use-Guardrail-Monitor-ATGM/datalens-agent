"""Structured service events with an explicit metadata allowlist."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO

from statistics_agent.core.settings import Settings

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class LogEvent(StrEnum):
    STARTED = "service_started"
    STOPPED = "service_stopped"
    HTTP_COMPLETED = "http_completed"
    HTTP_FAILED = "http_failed"


class ServiceLog:
    """Emit known events, never arbitrary messages, exceptions, bodies or headers.

    Route templates must come from server-owned routing metadata. This is not a
    general-purpose sanitizer for third-party logs or arbitrary application text.
    Each app owns its logger; configuring one app never changes root logging.
    """

    def __init__(self, settings: Settings, stream: TextIO | None = None) -> None:
        self._settings = settings
        self._logger = logging.Logger("datalens.service", level=settings.log_level)
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._logger.propagate = False

    def emit(
        self,
        event: LogEvent,
        *,
        status: int | None = None,
        duration_ms: float | None = None,
        route: str | None = None,
        method: str | None = None,
    ) -> None:
        level = logging.ERROR if event == LogEvent.HTTP_FAILED else logging.INFO
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": logging.getLevelName(level),
            "service": self._settings.service_name,
            "version": self._settings.service_version,
            "environment": self._settings.app_env,
            "event": LogEvent(event).value,
            "request_id": request_id_context.get(),
        }
        if status is not None:
            record["status"] = status
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 3)
        if route is not None:
            record["route"] = route
        if method is not None:
            record["method"] = method
        self._logger.log(level, json.dumps(record, allow_nan=False))
