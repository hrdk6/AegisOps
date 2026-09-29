"""Overview, system health, AI usage, evaluations, demo controls and event streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, AppState, Principal, require, session, state
from aegis.api.routes.incidents import action_view, incident_summary
from aegis.clients.controlplane import ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.db import audit
from aegis.db.models import (
    ActionRecord,
    ApprovalRecord,
    ComponentHeartbeat,
    EvaluationResult,
    EvaluationRun,
    Incident,
    ModelCall,
    ServiceStatus,
)
from aegis.domain.enums import IncidentStatus
from aegis.security.auth import Permission

router = APIRouter(tags=["platform"])
READ = Depends(require(Permission.READ))


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(st: AppState = Depends(state)) -> dict[str, str]:
    try:
        async with st.db.session() as s:
            await s.execute(text("SELECT 1"))
    except Exception as exc:
        raise APIError(503, "not_ready", "database unavailable") from exc
    return {"status": "ready"}


async def _probe(name: str, coro: Any) -> dict[str, Any]:
    t0 = asyncio.get_running_loop().time()
    try:
        ok = await asyncio.wait_for(coro, timeout=3)
        status = "ok" if ok else "down"
        detail = None
    except Exception as exc:
        status, detail = "down", type(exc).__name__
    return {"name": name, "status": status, "latency_ms": round((asyncio.get_running_loop().time() - t0) * 1000, 1),
            "detail": detail}


@router.get("/api/v1/system/health")
async def system_health(_: Principal = READ, st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    async def db_ok() -> bool:
        await s.execute(text("SELECT 1"))
        return True

    async def cp_ok() -> bool:
        return (await st.cp.health()).get("status") == "ok"

    async def chaos_ok() -> bool:
        return (await st.chaos.get("/healthz")).status_code == 200

    checks = await asyncio.gather(
        _probe("database", db_ok()), _probe("controller", cp_ok()), _probe("prometheus", st.prom.healthy()),
        _probe("loki", st.loki.healthy()), _probe("jaeger", st.jaeger.healthy()), _probe("chaos-injector", chaos_ok()))
    components = [{"name": "api", "status": "ok", "latency_ms": 0, "detail": None}, *checks]
    beats = {h.name: h for h in (await s.execute(select(ComponentHeartbeat))).scalars()}
    now = datetime.now(UTC)
    eng = beats.get("engine")
    if eng is None:
        components.append({"name": "engine", "status": "down", "detail": "no heartbeat"})
    else:
        age = (now - eng.updated_at).total_seconds()
        components.append({"name": "engine", "status": eng.status if age < 30 else "down",
                           "detail": f"heartbeat {age:.0f}s ago", "details": eng.details})
        routes = (eng.details or {}).get("llm_routes", {})
        components.append({"name": "ai-layer", "status": "ok" if age < 30 else "unknown",
                           "detail": "; ".join(f"{k}: {', '.join(v)}" for k, v in routes.items()) or "deterministic only",
                           "details": {"routes": routes}})
    sync = beats.get("audit-sync")
    components.append({"name": "execution-audit-sync", "status": "ok" if sync and (now - sync.updated_at).total_seconds() < 120
                       else "idle", "detail": f"cursor {sync.details}" if sync else "no controller audit events yet"})
    overall = "ok" if all(c["status"] in ("ok", "idle") for c in components) else "degraded"
    return {"status": overall, "components": components, "checked_at": now.isoformat()}


@router.get("/api/v1/overview")
async def overview(_: Principal = READ, st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    terminal = [IncidentStatus.RESOLVED.value, IncidentStatus.ESCALATED.value]
    active = (await s.execute(select(Incident).where(Incident.status.not_in(terminal))
                              .order_by(Incident.detected_at.desc()))).scalars().all()
    recent = (await s.execute(select(Incident).order_by(Incident.detected_at.desc()).limit(8))).scalars().all()
    services = (await s.execute(select(ServiceStatus).order_by(ServiceStatus.service))).scalars().all()
    actions = (await s.execute(select(ActionRecord).order_by(ActionRecord.created_at.desc()).limit(10))).scalars().all()
    pending = (await s.execute(select(func.count()).where(ApprovalRecord.status == "pending"))).scalar_one()
    since = datetime.now(UTC) - timedelta(hours=24)
    stats = (await s.execute(select(Incident.outcome, func.count()).where(Incident.detected_at >= since)
                             .group_by(Incident.outcome))).all()
    mttr = (await s.execute(select(func.avg(func.extract("epoch", Incident.resolved_at - Incident.onset_at)))
                            .where(Incident.detected_at >= since, Incident.resolved_at.is_not(None)))).scalar_one()
    risk_events = [a for a in actions if (a.decision or {}).get("allowed") is False or a.requires_approval]
    policy: dict[str, Any] = {}
    try:
        p = await st.cp.policy()
        policy = {"mode": p["spec"]["mode"], "breaker": p.get("circuitBreaker"), "source": p.get("source")}
    except (ControlPlaneError, UpstreamUnavailable):
        policy = {"mode": "unknown", "breaker": {"state": "unknown"}}
    http_services = [x for x in services if x.service in st.slos.services]
    healthy = sum(1 for x in http_services if x.status == "healthy")
    return {
        "services": [{"name": x.service, "status": x.status, "signals": x.signals} for x in services],
        "slo_compliance": round(healthy / len(http_services), 3) if http_services else None,
        "active_incidents": [incident_summary(i) for i in active],
        "recent_incidents": [incident_summary(i) for i in recent],
        "recent_actions": [action_view(a) for a in actions],
        "risk_events": [action_view(a) for a in risk_events],
        "pending_approvals": pending, "automation": policy,
        "last_24h": {"incidents_by_outcome": {str(o): n for o, n in stats}, "mttr_seconds": round(mttr, 1) if mttr else None},
    }


@router.get("/api/v1/system/ai")
async def ai_usage(_: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = (await s.execute(select(ModelCall.provider, ModelCall.model, ModelCall.status, func.count(),
                                   func.sum(ModelCall.prompt_tokens), func.sum(ModelCall.completion_tokens),
                                   func.sum(ModelCall.cost_usd), func.avg(ModelCall.latency_ms))
                            .group_by(ModelCall.provider, ModelCall.model, ModelCall.status))).all()
    recent = (await s.execute(select(ModelCall).order_by(ModelCall.id.desc()).limit(25))).scalars().all()
    return {"summary": [{"provider": r[0], "model": r[1], "status": r[2], "calls": r[3], "prompt_tokens": r[4] or 0,
                         "completion_tokens": r[5] or 0, "cost_usd": round(r[6] or 0, 6), "avg_latency_ms": round(r[7] or 0, 1)}
                        for r in rows],
            "recent": [{"id": c.id, "incident_id": c.incident_id, "purpose": c.purpose, "provider": c.provider,
                        "model": c.model, "status": c.status, "latency_ms": c.latency_ms, "prompt_tokens": c.prompt_tokens,
                        "completion_tokens": c.completion_tokens, "cost_usd": c.cost_usd, "attempt": c.attempt,
                        "fallback": c.fallback, "error": c.error, "created_at": c.created_at.isoformat()} for c in recent]}


@router.get("/api/v1/evaluations")
async def evaluations(_: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    runs = (await s.execute(select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(50))).scalars().all()
    return {"items": [{"id": r.id, "mode": r.mode, "status": r.status, "started_at": r.started_at.isoformat(),
                       "finished_at": r.finished_at.isoformat() if r.finished_at else None, "metadata": r.metadata_,
                       "summary": r.summary} for r in runs]}


@router.get("/api/v1/evaluations/{run_id}")
async def evaluation(run_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    run = await s.get(EvaluationRun, run_id)
    if run is None:
        raise APIError(404, "not_found", "evaluation run not found")
    results = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                               .order_by(EvaluationResult.scenario_id, EvaluationResult.repetition))).scalars().all()
    return {"id": run.id, "mode": run.mode, "status": run.status, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "metadata": run.metadata_,
            "summary": run.summary,
            "results": [{"scenario_id": r.scenario_id, "repetition": r.repetition, "incident_id": r.incident_id,
                         "passed": r.passed, "metrics": r.metrics, "details": r.details,
                         "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat()} for r in results]}


class RunCreate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]{3,40}$")
    mode: Literal["live", "replay"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResultCreate(BaseModel):
    scenario_id: str = Field(max_length=64)
    repetition: int = Field(ge=1, le=100)
    incident_id: str | None = None
    passed: bool
    metrics: dict[str, Any]
    details: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunComplete(BaseModel):
    summary: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal["completed", "failed"] = "completed"


@router.post("/api/v1/evaluations", status_code=201)
async def create_run(body: RunCreate, _: Principal = Depends(require(Permission.BENCHMARK)),
                     s: AsyncSession = Depends(session)) -> dict[str, Any]:
    if await s.get(EvaluationRun, body.id):
        raise APIError(409, "exists", "evaluation run already exists")
    s.add(EvaluationRun(id=body.id, mode=body.mode, status="running", started_at=datetime.now(UTC), metadata_=body.metadata))
    return {"id": body.id, "status": "running"}


@router.post("/api/v1/evaluations/{run_id}/results", status_code=201)
async def add_result(run_id: str, body: ResultCreate, _: Principal = Depends(require(Permission.BENCHMARK)),
                     s: AsyncSession = Depends(session)) -> dict[str, Any]:
    if await s.get(EvaluationRun, run_id) is None:
        raise APIError(404, "not_found", "evaluation run not found")
    now = datetime.now(UTC)
    s.add(EvaluationResult(run_id=run_id, scenario_id=body.scenario_id, repetition=body.repetition,
                           incident_id=body.incident_id, passed=body.passed, metrics=body.metrics, details=body.details,
                           started_at=body.started_at or now, finished_at=body.finished_at or now))
    return {"run_id": run_id, "scenario_id": body.scenario_id, "repetition": body.repetition}


@router.post("/api/v1/evaluations/{run_id}/complete")
async def complete_run(run_id: str, body: RunComplete, _: Principal = Depends(require(Permission.BENCHMARK)),
                       s: AsyncSession = Depends(session)) -> dict[str, Any]:
    run = await s.get(EvaluationRun, run_id)
    if run is None:
        raise APIError(404, "not_found", "evaluation run not found")
    run.status, run.summary, run.finished_at = body.status, body.summary, datetime.now(UTC)
    run.metadata_ = {**(run.metadata_ or {}), **body.metadata}
    return {"id": run_id, "status": run.status}


# ------------------------------------------------------------------ demo --
async def _chaos(st: AppState, method: str, path: str, **kw: Any) -> Any:
    if not st.settings.demo_mode:
        raise APIError(403, "demo_disabled", "demo mode is disabled in this environment")
    try:
        resp = await st.chaos.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise APIError(502, "chaos_unavailable", f"chaos injector unavailable: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise APIError(resp.status_code, "chaos_error", resp.json().get("detail", resp.text[:200]))
    return resp.json()


@router.get("/api/v1/demo/scenarios", tags=["demo"])
async def demo_scenarios(_: Principal = READ, st: AppState = Depends(state)) -> Any:
    return await _chaos(st, "GET", "/v1/scenarios")


@router.get("/api/v1/demo/status", tags=["demo"])
async def demo_status(_: Principal = READ, st: AppState = Depends(state)) -> Any:
    return await _chaos(st, "GET", "/v1/status")


@router.post("/api/v1/demo/scenarios/{scenario_id}/inject", tags=["demo"])
async def demo_inject(scenario_id: str, p: Principal = Depends(require(Permission.DEMO)), st: AppState = Depends(state),
                      s: AsyncSession = Depends(session)) -> Any:
    res = await _chaos(st, "POST", f"/v1/scenarios/{scenario_id}/inject")
    await audit.append(s, actor=p.username, action="demo.inject", resource=f"scenario/{scenario_id}", outcome="injected",
                       details={"result": res})
    return res


@router.post("/api/v1/demo/reset", tags=["demo"])
async def demo_reset(p: Principal = Depends(require(Permission.DEMO)), st: AppState = Depends(state),
                     s: AsyncSession = Depends(session)) -> Any:
    res = await _chaos(st, "POST", "/v1/reset")
    await audit.append(s, actor=p.username, action="demo.reset", resource="environment", outcome="reset", details={"result": res})
    return res


# ---------------------------------------------------------------- events --
@router.get("/api/v1/events", tags=["events"])
async def events(after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500), _: Principal = READ,
                 st: AppState = Depends(state)) -> dict[str, Any]:
    return {"items": await st.hub.backlog(after, limit)}


@router.get("/api/v1/events/stream", tags=["events"])
async def stream(request: Request, after: int = Query(0, ge=0), _: Principal = READ,
                 st: AppState = Depends(state)) -> StreamingResponse:
    """Server-sent events. Resume with ?after=<id> or the Last-Event-ID header."""
    last = int(request.headers.get("last-event-id") or after or 0)

    async def gen() -> AsyncIterator[str]:
        q = st.hub.subscribe()
        try:
            cursor = last
            if cursor == 0:  # new client: start from "now" rather than replaying history
                async with st.db.session() as s:
                    cursor = (await s.execute(text("SELECT coalesce(max(id), 0) FROM events"))).scalar_one()
            for ev in await st.hub.backlog(cursor):
                cursor = max(cursor, ev["id"])
                yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev["id"] <= cursor:
                    continue
                cursor = ev["id"]
                yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        finally:
            st.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
