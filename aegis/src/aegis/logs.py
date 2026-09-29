"""Structured JSON logging with trace correlation and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from aegis.security.redaction import redact, redact_obj

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
incident_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("incident_id", default=None)
action_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("action_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, var in (("request_id", request_id_var), ("incident_id", incident_id_var), ("action_id", action_id_var)):
            if (v := var.get()) is not None:
                doc[key] = v
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                doc["trace_id"] = format(ctx.trace_id, "032x")
                doc["span_id"] = format(ctx.span_id, "016x")
        except Exception:  # noqa: S110 - tracing is optional; logging must never fail
            pass
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            doc.update(redact_obj(fields))
        if record.exc_info and record.exc_info[0] is not None:
            doc["error_type"] = record.exc_info[0].__name__
            doc["exception"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps(doc, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio", "kubernetes.client.rest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def fields(**kw: Any) -> dict[str, Any]:
    """Structured fields: log.info("msg", extra=fields(event_type="x"))."""
    return {"fields": kw}
