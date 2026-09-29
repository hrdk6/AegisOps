"""aegis-api: authenticated REST + SSE API for humans, the UI and the CLI."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from aegis import __version__
from aegis.api.deps import APIError, AppState
from aegis.api.events import EventHub
from aegis.api.middleware import RequestContextMiddleware
from aegis.api.routes import approvals, auth, incidents, operations, platform, services
from aegis.clients.controlplane import ControlPlane
from aegis.clients.jaeger import Jaeger
from aegis.clients.loki import Loki
from aegis.clients.prometheus import Prometheus
from aegis.config import Settings, get_settings
from aegis.db import audit
from aegis.db.models import User
from aegis.db.session import Database
from aegis.engine.slo import SLOBook
from aegis.logs import request_id_var, setup_logging
from aegis.security.auth import hash_password, parse_bootstrap_users
from aegis.telemetry import setup_tracing

log = logging.getLogger("aegis.api")

DESCRIPTION = """
AegisOps control-plane API. Humans, the dashboard and the CLI use this API; the AI engine does not
(it writes incident state directly and talks to Kubernetes only through the Go control plane).

Authentication: `POST /api/v1/auth/login` returns a bearer token. Roles: viewer, operator, approver, admin.
"""


async def bootstrap_users(db: Database, spec: str) -> int:
    created = 0
    async with db.session() as s:
        for username, password, role in parse_bootstrap_users(spec):
            existing = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
            if existing is None:
                s.add(User(username=username, password_hash=hash_password(password), role=role))
                await audit.append(s, actor="system", action="user.bootstrap", resource=f"user/{username}",
                                   outcome="created", details={"role": role})
                created += 1
    return created


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.validate_for("api")
        db = Database(settings.dsn)
        hub = EventHub(db, settings.raw_dsn)
        st = AppState(
            settings=settings, db=db, cp=ControlPlane(settings.controlplane_url, settings.controlplane_bearer),
            prom=Prometheus(settings.prometheus_url), loki=Loki(settings.loki_url), jaeger=Jaeger(settings.jaeger_url),
            chaos=httpx.AsyncClient(base_url=settings.chaos_url, timeout=httpx.Timeout(20.0, connect=3.0),
                                    headers={"Authorization": f"Bearer {settings.chaos_token.get_secret_value()}"}),
            hub=hub, slos=SLOBook.load(settings.slo_file))
        app.state.aegis = st
        if settings.bootstrap_users.get_secret_value():
            n = await bootstrap_users(db, settings.bootstrap_users.get_secret_value())
            log.info("bootstrap users ensured", extra={"fields": {"created": n}})
        hub.start()
        yield
        await hub.stop()
        await st.chaos.aclose()
        for c in (st.cp, st.prom, st.loki, st.jaeger):
            await c.close()
        await db.dispose()

    app = FastAPI(title="AegisOps API", version=__version__, description=DESCRIPTION, lifespan=lifespan,
                  docs_url="/docs", redoc_url=None, openapi_url="/openapi.json")

    def error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
        body: dict[str, object] = {"error": {"code": code, "message": message}, "request_id": request_id_var.get()}
        if details is not None:
            body["error"]["details"] = details  # type: ignore[index]
        return JSONResponse(body, status_code=status)

    @app.exception_handler(APIError)
    async def api_error(_: Request, exc: APIError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return error(422, "validation_error", "request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return error(500, "internal_error", "internal server error")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for r in (auth.router, incidents.router, approvals.router, operations.router, services.router, platform.router):
        app.include_router(r)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID",
                                                                          "X-Request-ID"])
    app.add_middleware(RequestContextMiddleware)
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,events/stream")
    return app


def run() -> None:
    settings = get_settings()
    setup_logging("aegis-api", settings.log_level)
    setup_tracing("aegis-api", settings.otel_exporter_otlp_endpoint)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, log_level="warning", access_log=False,  # noqa: S104
                proxy_headers=True, forwarded_allow_ips="*")
