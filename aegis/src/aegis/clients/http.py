"""Shared HTTP helpers: bounded timeouts and typed failures."""

from __future__ import annotations

import httpx


class UpstreamUnavailable(Exception):
    """A dependency (Prometheus, Loki, Jaeger, control plane, ...) is unavailable."""

    def __init__(self, system: str, detail: str) -> None:
        super().__init__(f"{system} unavailable: {detail}")
        self.system = system
        self.detail = detail


def client(base_url: str, timeout: float = 5.0, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout, connect=2.0), headers=headers or {})
