"""Logging, metrics and tracing shared by every ShopFlow service."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

from shopflow.config import Settings

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

HTTP_REQUESTS = Counter(
    "shop_http_requests_total", "HTTP requests served", ["service", "route", "method", "code", "version"]
)
HTTP_DURATION = Histogram(
    "shop_http_request_duration_seconds", "HTTP request duration", ["service", "route", "version"],
    buckets=LATENCY_BUCKETS,
)
DEP_REQUESTS = Counter(
    "shop_dependency_requests_total", "Outbound dependency calls", ["service", "dependency", "outcome"]
)
DEP_DURATION = Histogram(
    "shop_dependency_request_duration_seconds", "Outbound dependency call duration", ["service", "dependency"],
    buckets=LATENCY_BUCKETS,
)
DB_POOL_SIZE = Gauge("shop_db_pool_size", "Configured DB pool size", ["service"])
DB_POOL_IN_USE = Gauge("shop_db_pool_in_use", "DB connections checked out", ["service"])
DB_POOL_WAITING = Gauge("shop_db_pool_waiting", "Requests waiting for a DB connection", ["service"])
DB_ACQUIRE_TIMEOUTS = Counter("shop_db_acquire_timeouts_total", "DB pool acquire timeouts", ["service"])
DB_ACQUIRE_SECONDS = Histogram(
    "shop_db_acquire_seconds", "Time to acquire a DB connection", ["service"], buckets=LATENCY_BUCKETS
)
CACHE_OPS = Counter("shop_cache_operations_total", "Cache operations", ["service", "outcome"])
BUILD_INFO = Gauge("shop_build_info", "Build information", ["service", "version"])


class JsonFormatter(logging.Formatter):
    """One JSON object per line, correlated with the active trace."""

    def __init__(self, service: str, version: str, pod: str) -> None:
        super().__init__()
        self.static = {"service": service, "version": version, "pod": pod}

    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "logger": record.name,
            **self.static,
        }
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                doc["trace_id"] = format(ctx.trace_id, "032x")
                doc["span_id"] = format(ctx.span_id, "016x")
        except Exception:  # pragma: no cover - tracing optional
            pass
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            doc.update(extra)
        if record.exc_info and record.exc_info[0] is not None:
            doc["error_type"] = record.exc_info[0].__name__
        return json.dumps(doc, default=str)


def setup_logging(settings: Settings) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.service, settings.version, settings.pod))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    BUILD_INFO.labels(settings.service, settings.version).set(1)
    return logging.getLogger(settings.service)


def setup_tracing(settings: Settings) -> None:
    """Configure OTLP trace export (no-op when disabled or unconfigured)."""
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true" or not settings.otlp_endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": settings.service,
        "service.version": settings.version,
        "service.namespace": "shopflow",
        "k8s.pod.name": settings.pod,
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()
    RedisInstrumentor().instrument()


def log_fields(**fields: object) -> dict[str, dict[str, object]]:
    """Helper for logger.info(msg, extra=log_fields(...))."""
    return {"fields": fields}
