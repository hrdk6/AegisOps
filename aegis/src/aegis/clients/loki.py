"""Loki query client."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from aegis.clients.http import UpstreamUnavailable, client


class Loki:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client(self.base_url, timeout=8.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 500) -> list[dict[str, Any]]:
        """Return log lines (newest first) as dicts: {ts, labels, line, fields}."""
        params = {"query": logql, "start": str(int(start.timestamp() * 1e9)), "end": str(int(end.timestamp() * 1e9)),
                  "limit": str(limit), "direction": "backward"}
        try:
            resp = await self.http.get("/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailable("loki", str(exc)) from exc
        out: list[dict[str, Any]] = []
        for stream in body.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                fields: dict[str, Any] = {}
                if line.startswith("{"):
                    try:
                        fields = json.loads(line)
                    except json.JSONDecodeError:
                        fields = {}
                out.append({"ts": int(ts), "labels": labels, "line": line, "fields": fields})
        out.sort(key=lambda r: r["ts"], reverse=True)
        return out

    async def healthy(self) -> bool:
        try:
            return (await self.http.get("/ready")).status_code == 200
        except httpx.HTTPError:
            return False
