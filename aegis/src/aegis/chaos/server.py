"""aegis-chaos: token-protected HTTP front end for the fault injector (demo only)."""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

from aegis.chaos.injector import ChaosError, Injector
from aegis.chaos.scenarios import load_scenarios
from aegis.config import get_settings
from aegis.logs import fields, setup_logging

log = logging.getLogger("aegis.chaos.server")
MIN_INTERVAL_S = 10.0


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_for("chaos")
    scenarios = load_scenarios(settings.scenario_dir)
    state: dict[str, Any] = {"last_injection": 0.0}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        state["injector"] = Injector(settings.managed_namespace, Path("/app/deploy/shop"),
                                     "http://shop-network.shop.svc:8474", "http://loadgen.traffic.svc:8089")
        yield

    app = FastAPI(title="AegisOps chaos injector", lifespan=lifespan, docs_url=None, redoc_url=None)

    def auth(authorization: str = Header(default="")) -> None:
        token = settings.chaos_token.get_secret_value()
        if not token or not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), token):
            raise HTTPException(401, "unauthorized")
        if not settings.demo_mode:
            raise HTTPException(403, "demo mode disabled")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/scenarios", dependencies=[Depends(auth)])
    async def list_scenarios() -> dict[str, Any]:
        return {"items": [{"id": s.id, "title": s.title, "fault_class": s.fault_class, "description": s.description,
                           "fault_type": s.fault.type, "target": s.fault.target, "demo": s.demo, "tags": s.tags}
                          for s in scenarios.values()]}

    @app.get("/v1/scenarios/{scenario_id}", dependencies=[Depends(auth)])
    async def get_scenario(scenario_id: str) -> dict[str, Any]:
        if scenario_id not in scenarios:
            raise HTTPException(404, "unknown scenario")
        return scenarios[scenario_id].model_dump()

    @app.post("/v1/scenarios/{scenario_id}/inject", dependencies=[Depends(auth)])
    async def inject(scenario_id: str) -> dict[str, Any]:
        sc = scenarios.get(scenario_id)
        if sc is None:
            raise HTTPException(404, "unknown scenario")
        if time.monotonic() - state["last_injection"] < MIN_INTERVAL_S:
            raise HTTPException(429, "injections are rate limited")
        state["last_injection"] = time.monotonic()
        try:
            result = await state["injector"].inject(sc)
        except ChaosError as exc:
            raise HTTPException(409, str(exc)) from exc
        log.info("fault injected", extra=fields(scenario=scenario_id, result=result))
        return {"scenario": scenario_id, **result}

    @app.post("/v1/reset", dependencies=[Depends(auth)])
    async def reset() -> dict[str, Any]:
        try:
            result = await state["injector"].reset()
        except ChaosError as exc:
            raise HTTPException(409, str(exc)) from exc
        log.info("environment reset", extra=fields(result=result))
        return result

    @app.get("/v1/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, Any]:
        return await state["injector"].status()

    return app


def run() -> None:
    settings = get_settings()
    setup_logging("aegis-chaos", settings.log_level)
    uvicorn.run(create_app(), host="0.0.0.0", port=8090, log_level="warning", access_log=False)  # noqa: S104
