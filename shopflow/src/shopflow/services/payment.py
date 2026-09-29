"""Payment service: fraud scoring (CPU-bound) and ledger writes."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI

from shopflow import behaviors
from shopflow.app import Runtime, build_app
from shopflow.errors import BadRequestError, ServiceError

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    currency TEXT NOT NULL,
    risk_score REAL NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def register(app: FastAPI, rt: Runtime) -> None:
    @app.post("/charge")
    async def charge(body: dict[str, Any]) -> dict[str, Any]:
        amount = float(body.get("amount", 0))
        currency = str(body.get("currency", "USD"))
        order_id = str(body.get("order_id", ""))[:64]
        if amount <= 0 or not order_id:
            raise BadRequestError("charge requires order_id and positive amount")
        # Fraud scoring runs inline on the event loop (CPU-bound by design).
        behaviors.cpu_work(behaviors.fraud_model_cost_ms())
        if behaviors.rounding_mismatch():
            ledger = round(amount - 0.01, 2)
            raise ServiceError(f"payment processing failed: RoundingMismatchError: ledger total {ledger:.2f} "
                               f"!= charge {amount:.2f}", status=500, error_type="RoundingMismatchError")
        payment_id = uuid.uuid4().hex[:16]
        assert rt.db is not None
        await rt.db.execute(
            "INSERT INTO payments (id, order_id, amount, currency, risk_score) VALUES ($1, $2, $3, $4, $5)",
            payment_id, order_id, amount, currency, 0.12)
        return {"id": payment_id, "status": "captured"}


def create_app() -> FastAPI:
    return build_app("payment-service", register, schema=SCHEMA)
