"""FastAPI application factory shared by every ShopFlow service."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shopflow.config import Settings
from shopflow.datastores import Cache, Database
from shopflow.errors import ServiceError
from shopflow.telemetry import HTTP_DURATION, HTTP_REQUESTS, log_fields, setup_logging, setup_tracing
from shopflow.upstream import Upstreams

UNINSTRUMENTED = {"/healthz", "/readyz", "/metrics"}


@dataclass
class Runtime:
    """Per-process dependencies handed to route handlers."""

    settings: Settings
    log: logging.Logger
    upstreams: Upstreams
    db: Database | None = None
    cache: Cache | None = None
    ready: bool = False
    state: dict[str, Any] = field(default_factory=dict)


class MetricsMiddleware:
    """Pure-ASGI middleware recording RED metrics and access logs."""

    def __init__(self, app: ASGIApp, runtime: Runtime) -> None:
        self.app = app
        self.rt = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in UNINSTRUMENTED:
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            route = scope.get("route")
            template = getattr(route, "path", "unmatched")
            s = self.rt.settings
            HTTP_REQUESTS.labels(s.service, template, scope["method"], str(status["code"]), s.version).inc()
            HTTP_DURATION.labels(s.service, template, s.version).observe(duration)
            level = logging.ERROR if status["code"] >= 500 else logging.INFO
            self.rt.log.log(level, "request completed", extra=log_fields(
                route=template, method=scope["method"], status=status["code"], duration_ms=round(duration * 1000, 1)))


def build_app(
    service: str,
    register: Callable[[FastAPI, Runtime], None],
    *,
    schema: str = "",
    startup: Callable[[Runtime], Awaitable[None]] | None = None,
) -> FastAPI:
    settings = Settings.from_env(service)
    log = setup_logging(settings)
    setup_tracing(settings)
    rt = Runtime(settings=settings, log=log, upstreams=Upstreams(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.uses_postgres:
            rt.db = Database(settings)
            await rt.db.connect(schema)
        if settings.uses_redis:
            rt.cache = Cache(settings)
        if startup is not None:
            await startup(rt)
        rt.ready = True
        log.info("service started", extra=log_fields(sandbox=settings.sandbox, upstreams=list(settings.upstreams)))
        yield
        await rt.upstreams.close()
        if rt.db:
            await rt.db.close()
        if rt.cache:
            await rt.cache.close()

    app = FastAPI(title=f"ShopFlow {service}", version=settings.version, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        log.log(logging.ERROR if exc.status >= 500 else logging.WARNING, str(exc),
                extra=log_fields(error_type=exc.error_type, path=request.url.path, status=exc.status))
        return JSONResponse({"error": str(exc), "type": exc.error_type}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error(f"unhandled error: {type(exc).__name__}: {exc}", exc_info=exc,
                  extra=log_fields(error_type=type(exc).__name__, path=request.url.path))
        return JSONResponse({"error": "internal error", "type": type(exc).__name__}, status_code=500)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        return JSONResponse({"ready": rt.ready}, status_code=200 if rt.ready else 503)

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    register(app, rt)
    # Metrics middleware is added first so the OpenTelemetry middleware wraps it
    # and access logs carry the request's trace_id.
    app.add_middleware(MetricsMiddleware, runtime=rt)
    if settings.otlp_endpoint and not settings.sandbox:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    return app
