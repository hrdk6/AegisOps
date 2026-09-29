"""Prometheus HTTP API client (instant and range queries)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx

from aegis.clients.http import UpstreamUnavailable, client


class Prometheus:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client(self.base_url, timeout=6.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.http.get(path, params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailable("prometheus", str(exc)) from exc
        if body.get("status") != "success":
            raise UpstreamUnavailable("prometheus", body.get("error", "query failed"))
        return body["data"]

    async def vector(self, query: str) -> list[tuple[dict[str, str], float]]:
        """Instant query → [(labels, value)] with NaN/Inf dropped."""
        data = await self._get("/api/v1/query", {"query": query})
        out = []
        for r in data.get("result", []):
            v = float(r["value"][1])
            if not (math.isnan(v) or math.isinf(v)):
                out.append((r["metric"], v))
        return out

    async def by_label(self, query: str, label: str) -> dict[str, float]:
        return {labels.get(label, ""): v for labels, v in await self.vector(query)}

    async def range(self, query: str, start: datetime, end: datetime, step: str = "10s") -> list[dict[str, Any]]:
        data = await self._get("/api/v1/query_range", {"query": query, "start": start.timestamp(),
                                                        "end": end.timestamp(), "step": step})
        series = []
        for r in data.get("result", []):
            points = [(float(t), float(v)) for t, v in r["values"] if not math.isnan(float(v))]
            series.append({"labels": r["metric"], "points": points})
        return series

    async def healthy(self) -> bool:
        try:
            resp = await self.http.get("/-/ready")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
