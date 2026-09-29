"""Auth service: sessions in Redis, users in PostgreSQL.

Configuration is read from AUTH_CONFIG (JSON, typically from the auth-config
ConfigMap) and validated at startup; invalid configuration aborts startup.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any

from fastapi import FastAPI

from shopflow import behaviors
from shopflow.app import Runtime, build_app
from shopflow.errors import BadRequestError

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    username TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login TIMESTAMPTZ
);
"""


class ConfigurationError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    raw = os.environ.get("AUTH_CONFIG", '{"token_ttl_seconds": 3600, "issuer": "shopflow"}')
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"configuration invalid: AUTH_CONFIG is not valid JSON ({exc.msg} at pos {exc.pos})") from exc
    ttl = cfg.get("token_ttl_seconds")
    if not isinstance(ttl, int) or not 60 <= ttl <= 86400:
        raise ConfigurationError("configuration invalid: token_ttl_seconds must be an integer in [60, 86400]")
    if not cfg.get("issuer"):
        raise ConfigurationError("configuration invalid: issuer is required")
    return cfg


def register(app: FastAPI, rt: Runtime) -> None:
    cfg = load_config()  # fail fast at import/startup on bad configuration

    @app.post("/login")
    async def login(body: dict[str, Any]) -> dict[str, Any]:
        user = str(body.get("username", "")).strip()[:64]
        if not user:
            raise BadRequestError("username required")
        assert rt.db is not None and rt.cache is not None
        await rt.db.execute(
            "INSERT INTO auth_users (username, last_login) VALUES ($1, now()) "
            "ON CONFLICT (username) DO UPDATE SET last_login = now()", user)
        token = secrets.token_urlsafe(24)
        await rt.cache.set(f"session:{token}", user, cfg["token_ttl_seconds"])
        return {"token": token, "user": user, "issuer": cfg["issuer"]}

    @app.post("/verify")
    async def verify(body: dict[str, Any]) -> dict[str, Any]:
        token = str(body.get("token", ""))
        delay = behaviors.session_validation_delay()
        if delay:
            await asyncio.sleep(delay)  # strict-remote validation round trip
        assert rt.cache is not None
        user = await rt.cache.get(f"session:{token}")
        return {"valid": user is not None, "user": user}


def create_app() -> FastAPI:
    return build_app("auth-service", register, schema=SCHEMA)
