"""Jaeger query API client."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from aegis.clients.http import UpstreamUnavailable, client


class Jaeger:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client(self.base_url, timeout=8.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self.http.get(path, params=params)
            resp.raise_for_status()
            return resp.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailable("jaeger", str(exc)) from exc

    async def traces(self, service: str, start: datetime, end: datetime, *, errors_only: bool = False,
                     min_duration_ms: int | None = None, limit: int = 40) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"service": service, "start": int(start.timestamp() * 1e6),
                                  "end": int(end.timestamp() * 1e6), "limit": limit}
        if errors_only:
            params["tags"] = json.dumps({"error": "true"})
        if min_duration_ms:
            params["minDuration"] = f"{min_duration_ms}ms"
        return await self._get("/api/traces", params)

    async def dependencies(self, end: datetime, lookback_ms: int = 900_000) -> list[dict[str, Any]]:
        return await self._get("/api/dependencies", {"endTs": int(end.timestamp() * 1000), "lookback": lookback_ms})

    async def healthy(self) -> bool:
        try:
            return (await self.http.get("/api/services")).status_code == 200
        except httpx.HTTPError:
            return False
