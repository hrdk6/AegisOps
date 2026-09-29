"""Request IDs, structured access logs, security headers and rate limiting."""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aegis.logs import request_id_var

log = logging.getLogger("aegis.api.access")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode()[:64] or secrets.token_hex(8)
        request_id_var.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status = {"code": 500}

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                h = list(message.get("headers", []))
                h.append((b"x-request-id", rid.encode()))
                if scope["path"] not in ("/docs", "/openapi.json", "/redoc"):
                    h.extend(SECURITY_HEADERS)
                message["headers"] = h
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            if scope["path"] not in ("/healthz", "/readyz", "/metrics"):
                log.info("request", extra={"fields": {
                    "method": scope["method"], "path": scope["path"], "status": status["code"],
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1)}})


class RateLimiter:
    """Sliding-window limiter keyed by client identity."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 10000:
            self.hits.clear()
        return True
