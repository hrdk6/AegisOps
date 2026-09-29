from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, AppState, Principal, principal, session, state
from aegis.db import audit
from aegis.db.models import User
from aegis.security.auth import DUMMY_HASH, issue_token, permissions_for, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: dict[str, Any]


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, st: AppState = Depends(state),
                s: AsyncSession = Depends(session)) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    if not st.limiter.allow(f"login:{ip}", st.settings.login_attempts_per_minute):
        raise APIError(429, "rate_limited", "too many login attempts; try again later")
    user = (await s.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH) and user is not None and not user.disabled
    await audit.append(s, actor=body.username, action="auth.login", resource="session", outcome="success" if ok else "failure",
                       details={"ip": ip})
    if not ok or user is None:
        raise APIError(401, "invalid_credentials", "invalid username or password")
    token, exp = issue_token(st.settings.jwt_secret.get_secret_value(), user.username, user.role, st.settings.jwt_ttl_minutes)
    return LoginResponse(access_token=token, expires_at=exp, user={
        "username": user.username, "role": user.role, "permissions": sorted(p.value for p in permissions_for(user.role))})


@router.get("/me")
async def me(p: Principal = Depends(principal)) -> dict[str, Any]:
    return {"username": p.username, "role": p.role, "permissions": sorted(x.value for x in p.permissions)}
