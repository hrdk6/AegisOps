"""Notification service: renders and "sends" order e-mails."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI

from shopflow import behaviors
from shopflow.app import Runtime, build_app
from shopflow.errors import BadRequestError


def register(app: FastAPI, rt: Runtime) -> None:
    templates: OrderedDict[str, str] = OrderedDict()
    sent = {"count": 0}

    @app.post("/notify")
    async def notify(body: dict[str, Any]) -> dict[str, Any]:
        order_id = str(body.get("order_id", ""))[:64]
        if not order_id:
            raise BadRequestError("order_id required")
        leak_kb = behaviors.template_cache_leak_kb()
        if leak_kb:
            behaviors.retain_memory(leak_kb)  # rendered templates cached without eviction
        else:
            templates[order_id] = f"Order {order_id} confirmed"
            while len(templates) > 256:
                templates.popitem(last=False)
        await asyncio.sleep(0.02)  # SMTP hand-off
        sent["count"] += 1
        return {"queued": True, "order_id": order_id}


def create_app() -> FastAPI:
    return build_app("notification-service", register)
