"""Order service: orchestrates inventory reservation, payment and notification."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI

from shopflow import behaviors
from shopflow.app import Runtime, build_app
from shopflow.errors import BadRequestError, CacheUnavailableError, ServiceError
from shopflow.telemetry import log_fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

log = logging.getLogger("order-service")


def register(app: FastAPI, rt: Runtime) -> None:
    background: set[asyncio.Task[Any]] = set()

    async def notify(order_id: str, user: str) -> None:
        try:
            await rt.upstreams.json("notification-service", "POST", "/notify", json={"order_id": order_id, "user": user})
        except ServiceError as exc:
            # Notifications are best-effort: the order already succeeded.
            log.warning("order notification failed", extra=log_fields(order_id=order_id, error_type=exc.error_type,
                                                                       error=str(exc)))

    @app.post("/orders")
    async def create_order(body: dict[str, Any]) -> dict[str, Any]:
        items = body.get("items") or []
        amount = float(body.get("amount", 0))
        user = str(body.get("user", "anonymous"))[:64]
        if not items or amount <= 0:
            raise BadRequestError("order requires items and a positive amount")
        order_id = uuid.uuid4().hex[:16]

        delay = behaviors.sync_fraud_check_delay()
        if delay:
            await asyncio.sleep(delay)  # synchronous pre-authorization round trip

        await rt.upstreams.json("inventory-service", "POST", "/reserve", json={"order_id": order_id, "items": items})
        payment = await rt.upstreams.json("payment-service", "POST", "/charge",
                                          json={"order_id": order_id, "amount": amount, "currency": "USD"})
        assert rt.db is not None
        leak = behaviors.reconcile_leaks_connection()
        async with rt.db.connection(leak=leak) as conn:
            await conn.execute("INSERT INTO orders (id, username, amount, status) VALUES ($1, $2, $3, 'paid')",
                               order_id, user, amount)
            if leak:
                # Inline reconciliation: open a transaction that is never closed.
                await conn.execute("BEGIN")
                await conn.execute("SELECT id FROM orders WHERE id = $1 FOR UPDATE", order_id)
        if rt.cache is not None:
            try:
                await rt.cache.set(f"order:{order_id}", json.dumps({"id": order_id, "status": "paid",
                                                                    "amount": amount}), 600)
            except CacheUnavailableError as exc:
                log.warning("order cache write failed", extra=log_fields(order_id=order_id, error=str(exc)))
        task = asyncio.create_task(notify(order_id, user))
        background.add(task)
        task.add_done_callback(background.discard)
        return {"id": order_id, "status": "paid", "payment": payment.get("id")}

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str) -> dict[str, Any]:
        if rt.cache is not None:
            cached = await rt.cache.get(f"order:{order_id}")
            if cached:
                return json.loads(cached)
        assert rt.db is not None
        rows = await rt.db.fetch("SELECT id, status, amount FROM orders WHERE id = $1", order_id)
        if not rows:
            raise ServiceError("order not found", status=404, error_type="NotFound")
        r = rows[0]
        return {"id": r["id"], "status": r["status"], "amount": float(r["amount"])}


def create_app() -> FastAPI:
    return build_app("order-service", register, schema=SCHEMA)
