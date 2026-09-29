"""Inventory service: product catalog (cached) and stock reservation."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI

from shopflow.app import Runtime, build_app
from shopflow.config import flag
from shopflow.errors import BadRequestError, CacheUnavailableError
from shopflow.telemetry import log_fields

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "INR"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price NUMERIC(12,2) NOT NULL,
    stock INTEGER NOT NULL
);
INSERT INTO products (sku, name, price, stock) VALUES
    ('SKU-1', 'Trail Running Shoes', 89.99, 100000),
    ('SKU-2', 'Merino Base Layer', 64.50, 100000),
    ('SKU-3', 'Insulated Bottle', 24.00, 100000),
    ('SKU-4', 'Headlamp 400lm', 39.95, 100000)
ON CONFLICT (sku) DO NOTHING;
"""

log = logging.getLogger("inventory-service")


def register(app: FastAPI, rt: Runtime) -> None:
    currency = flag("CATALOG_CURRENCY", "USD")
    if currency not in SUPPORTED_CURRENCIES:
        raise RuntimeError(f"configuration invalid: unsupported currency {currency!r} in CATALOG_CURRENCY "
                           f"(supported: {sorted(SUPPORTED_CURRENCIES)})")

    @app.get("/products")
    async def products() -> dict[str, Any]:
        if rt.cache is not None:
            try:
                cached = await rt.cache.get("catalog:v1")
                if cached:
                    return {"products": json.loads(cached), "currency": currency, "cached": True}
            except CacheUnavailableError as exc:
                log.warning("catalog cache unavailable; reading from database", extra=log_fields(error=str(exc)))
        assert rt.db is not None
        rows = await rt.db.fetch("SELECT sku, name, price FROM products ORDER BY sku")
        items = [{"sku": r["sku"], "name": r["name"], "price": float(r["price"])} for r in rows]
        if rt.cache is not None:
            try:
                await rt.cache.set("catalog:v1", json.dumps(items), 10)
            except CacheUnavailableError:
                pass
        return {"products": items, "currency": currency, "cached": False}

    @app.post("/reserve")
    async def reserve(body: dict[str, Any]) -> dict[str, Any]:
        items = body.get("items") or []
        if not items:
            raise BadRequestError("items required")
        assert rt.db is not None
        async with rt.db.connection() as conn:
            async with conn.transaction():
                for item in items[:20]:
                    await conn.execute("UPDATE products SET stock = stock - $2 WHERE sku = $1 AND stock >= $2",
                                       str(item.get("sku")), int(item.get("qty", 1)))
        return {"reserved": True, "order_id": body.get("order_id")}


def create_app() -> FastAPI:
    return build_app("inventory-service", register, schema=SCHEMA)
