"""API gateway: routing plus token verification for authenticated routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header

from shopflow import behaviors
from shopflow.app import Runtime, build_app
from shopflow.errors import ServiceError, UnauthorizedError


def register(app: FastAPI, rt: Runtime) -> None:
    async def authenticate(authorization: str) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise UnauthorizedError("missing bearer token")
        result = await rt.upstreams.json("auth-service", "POST", "/verify", json={"token": token})
        if not result.get("valid"):
            raise UnauthorizedError("invalid session")
        return str(result.get("user", "unknown"))

    @app.get("/api/products")
    async def products() -> dict[str, Any]:
        return await rt.upstreams.json("inventory-service", "GET", "/products")

    @app.post("/api/auth/login")
    async def login(body: dict[str, Any]) -> dict[str, Any]:
        return await rt.upstreams.json("auth-service", "POST", "/login", json=body)

    @app.post("/api/orders")
    async def create_order(body: dict[str, Any], authorization: str = Header(default="")) -> dict[str, Any]:
        user = await authenticate(authorization)
        if behaviors.routing_table_miss():
            raise ServiceError("no healthy upstream for route POST /api/orders (routing table v2)", status=503,
                               error_type="RouteNotFound")
        return await rt.upstreams.json("order-service", "POST", "/orders", json={**body, "user": user})

    @app.get("/api/orders/{order_id}")
    async def get_order(order_id: str, authorization: str = Header(default="")) -> dict[str, Any]:
        await authenticate(authorization)
        return await rt.upstreams.json("order-service", "GET", f"/orders/{order_id}")


def create_app() -> FastAPI:
    return build_app("api-gateway", register)
