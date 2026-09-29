"""Instrumented HTTP client for calling other ShopFlow services."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from shopflow.config import Settings
from shopflow.errors import DependencyError, ServiceError
from shopflow.telemetry import DEP_DURATION, DEP_REQUESTS, log_fields

log = logging.getLogger("shopflow.upstream")


class Upstreams:
    """Client-side view of every HTTP dependency.

    Metrics are recorded from the *caller's* perspective, which is what lets an
    operator distinguish a slow dependency (server-side latency high) from a
    degraded network path (client-side latency high, server-side normal).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout, connect=1.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def call(
        self, dependency: str, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        service = self.settings.service
        if self.settings.sandbox:
            # Sandbox contract: downstream services are stubbed so a simulation
            # exercises only this service's own code and configuration.
            await asyncio.sleep(0.005)
            return httpx.Response(200, json={"stub": True, "ok": True, "id": "sandbox", "token": "sandbox",
                                             "valid": True, "user": "sandbox", "products": []})
        base = self.settings.upstreams.get(dependency)
        if base is None:
            raise ServiceError(f"no route to dependency {dependency}")
        start = time.perf_counter()
        outcome = "ok"
        try:
            resp = await self.client.request(method, base + path, json=json, headers=headers)
            if resp.status_code >= 500:
                outcome = "error"
            return resp
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            log.warning("upstream call timed out", extra=log_fields(dependency=dependency, path=path,
                                                                     error_type=type(exc).__name__))
            raise DependencyError(f"upstream {dependency} timed out: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            outcome = "error"
            log.warning("upstream call failed", extra=log_fields(dependency=dependency, path=path,
                                                                  error_type=type(exc).__name__, error=str(exc)[:200]))
            raise DependencyError(f"upstream {dependency} unreachable: {type(exc).__name__}: {str(exc)[:120]}") from exc
        finally:
            DEP_REQUESTS.labels(service, dependency, outcome).inc()
            DEP_DURATION.labels(service, dependency).observe(time.perf_counter() - start)

    async def json(self, dependency: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self.call(dependency, method, path, **kwargs)
        if resp.status_code >= 500:
            raise DependencyError(f"upstream {dependency} returned {resp.status_code}")
        if resp.status_code >= 400:
            detail = resp.json().get("error", resp.text[:120]) if resp.headers.get("content-type", "").startswith(
                "application/json") else resp.text[:120]
            raise ServiceError(str(detail), status=resp.status_code, error_type="UpstreamRejected")
        return resp.json()
