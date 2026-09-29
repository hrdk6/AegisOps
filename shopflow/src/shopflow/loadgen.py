"""Synthetic user traffic ("the internet") with a small control API.

The load generator runs outside the managed namespace, so traffic changes it
makes are invisible to the AegisOps change log, exactly like a real surge.
It also measures the customer-facing success rate, which AegisOps uses as the
customer-impact signal in postmortems.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from shopflow.telemetry import LATENCY_BUCKETS

log = logging.getLogger("loadgen")

REQS = Counter("loadgen_requests_total", "Synthetic user requests", ["flow", "outcome"])
DUR = Histogram("loadgen_request_duration_seconds", "Synthetic user request latency", ["flow"], buckets=LATENCY_BUCKETS)
RATE = Gauge("loadgen_target_rps", "Configured request rate", ["flow"])

BASE_RATES = {"browse": float(os.environ.get("BROWSE_RPS", "6")), "checkout": float(os.environ.get("CHECKOUT_RPS", "3")),
              "login": float(os.environ.get("LOGIN_RPS", "0.3"))}


class Control(BaseModel):
    multiplier: float = Field(default=1.0, ge=0.0, le=10.0)
    checkout_multiplier: float = Field(default=1.0, ge=0.0, le=10.0)


class Generator:
    def __init__(self, target: str) -> None:
        self.target = target.rstrip("/")
        self.control = Control()
        self.tokens: list[str] = []
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.0),
                                        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        self.sem = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENCY", "150")))
        self.tasks: set[asyncio.Task[Any]] = set()

    def rate(self, flow: str) -> float:
        r = BASE_RATES[flow] * self.control.multiplier
        if flow == "checkout":
            r *= self.control.checkout_multiplier
        return r

    async def _request(self, flow: str, method: str, path: str, **kw: Any) -> httpx.Response | None:
        async with self.sem:
            start = time.perf_counter()
            outcome = "error"
            try:
                resp = await self.client.request(method, self.target + path, **kw)
                outcome = "success" if resp.status_code < 500 else "error"
                if resp.status_code in (401, 403) and flow == "checkout":
                    outcome = "success"  # session expiry is not a platform failure
                return resp
            except httpx.HTTPError:
                return None
            finally:
                REQS.labels(flow, outcome).inc()
                DUR.labels(flow).observe(time.perf_counter() - start)

    async def login(self) -> None:
        resp = await self._request("login", "POST", "/login", json={"username": f"user-{random.randint(1, 500)}"})
        if resp is not None and resp.status_code == 200:
            self.tokens.append(resp.json()["token"])
            del self.tokens[:-50]

    async def browse(self) -> None:
        await self._request("browse", "GET", "/products")

    async def checkout(self) -> None:
        if not self.tokens:
            await self.login()
            return
        sku = random.choice(["SKU-1", "SKU-2", "SKU-3", "SKU-4"])
        resp = await self._request("checkout", "POST", "/checkout",
                                   json={"items": [{"sku": sku, "qty": 1}], "amount": round(random.uniform(20, 120), 2)},
                                   headers={"Authorization": f"Bearer {random.choice(self.tokens)}"})
        if resp is not None and resp.status_code == 401:
            self.tokens.clear()

    async def drive(self, flow: str) -> None:
        fn = getattr(self, flow)
        while True:
            rate = self.rate(flow)
            RATE.labels(flow).set(rate)
            if rate <= 0:
                await asyncio.sleep(1)
                continue
            task = asyncio.create_task(fn())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            await asyncio.sleep(random.expovariate(rate))  # Poisson arrivals


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","msg":"%(message)s","service":"loadgen"}')
    gen = Generator(os.environ.get("TARGET_URL", "http://storefront.shop.svc:8080"))
    runners: list[asyncio.Task[Any]] = []

    async def start() -> None:
        await asyncio.sleep(float(os.environ.get("STARTUP_DELAY", "5")))
        for flow in BASE_RATES:
            runners.append(asyncio.create_task(gen.drive(flow)))
        log.info("load generation started")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        starter = asyncio.create_task(start())
        yield
        starter.cancel()
        for r in runners:
            r.cancel()
        await gen.client.aclose()

    app = FastAPI(title="ShopFlow load generator", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return {"control": gen.control.model_dump(), "rates": {f: gen.rate(f) for f in BASE_RATES},
                "inflight": len(gen.tasks)}

    @app.post("/control")
    async def control(body: Control) -> dict[str, Any]:
        if not 0 <= body.multiplier <= 10:
            raise HTTPException(400, "multiplier out of range")
        gen.control = body
        log.info(f"traffic profile changed: {body.model_dump()}")
        return await status()

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
