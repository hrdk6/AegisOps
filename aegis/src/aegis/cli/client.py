"""Thin authenticated client for the AegisOps API (used by the CLI and the benchmark runner)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path(os.environ.get("AEGIS_CLI_CONFIG", Path.home() / ".aegis" / "cli.json"))


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        msg = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status
        self.body = body


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @classmethod
    def from_config(cls, url: str | None = None) -> ApiClient:
        cfg = load_config()
        return cls(url or os.environ.get("AEGIS_API_URL") or cfg.get("url") or "http://localhost:8000",
                   os.environ.get("AEGIS_TOKEN") or cfg.get("token"))

    async def close(self) -> None:
        await self.http.aclose()

    async def login(self, username: str, password: str) -> dict[str, Any]:
        body = await self.request("POST", "/api/v1/auth/login", json={"username": username, "password": password}, auth=False)
        self.token = body["access_token"]
        return body

    async def request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                      auth: bool = True, raw: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        resp = await self.http.request(method, path, json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise ApiError(resp.status_code, body)
        if raw:
            return resp.text
        return resp.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v is not None} or None)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self.request("POST", path, json=body if body is not None else {})

    async def put(self, path: str, body: Any) -> Any:
        return await self.request("PUT", path, json=body)


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
