"""Application state, authentication and authorization dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.events import EventHub
from aegis.api.middleware import RateLimiter
from aegis.clients.controlplane import ControlPlane
from aegis.clients.jaeger import Jaeger
from aegis.clients.loki import Loki
from aegis.clients.prometheus import Prometheus
from aegis.config import Settings
from aegis.db.models import User
from aegis.db.session import Database
from aegis.engine.slo import SLOBook
from aegis.security.auth import Permission, decode_token, permissions_for


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


@dataclass
class AppState:
    settings: Settings
    db: Database
    cp: ControlPlane
    prom: Prometheus
    loki: Loki
    jaeger: Jaeger
    chaos: httpx.AsyncClient
    hub: EventHub
    slos: SLOBook
    limiter: RateLimiter = field(default_factory=RateLimiter)


@dataclass
class Principal:
    username: str
    role: str
    permissions: set[Permission]


def state(request: Request) -> AppState:
    return request.app.state.aegis  # type: ignore[no-any-return]


async def session(st: AppState = Depends(state)) -> AsyncIterator[AsyncSession]:
    async with st.db.session() as s:
        yield s


async def principal(request: Request, st: AppState = Depends(state)) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise APIError(401, "unauthenticated", "missing bearer token")
    try:
        claims = decode_token(st.settings.jwt_secret.get_secret_value(), header[7:].strip())
    except jwt.PyJWTError as exc:
        raise APIError(401, "unauthenticated", "invalid or expired token") from exc
    async with st.db.session() as s:
        user = (await s.execute(select(User).where(User.username == claims["sub"]))).scalar_one_or_none()
    if user is None or user.disabled or user.role != claims["role"]:
        raise APIError(401, "unauthenticated", "account disabled or changed")
    if not st.limiter.allow(f"user:{user.username}", st.settings.rate_limit_per_minute):
        raise APIError(429, "rate_limited", "too many requests")
    return Principal(username=user.username, role=user.role, permissions=permissions_for(user.role))


def require(permission: Permission) -> Callable[..., Any]:
    async def dep(p: Principal = Depends(principal)) -> Principal:
        if permission not in p.permissions:
            raise APIError(403, "forbidden", f"permission '{permission.value}' required (role {p.role})")
        return p

    return dep
