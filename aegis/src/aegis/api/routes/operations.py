"""Actions, simulations, policy and audit endpoints."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, AppState, Principal, require, session, state
from aegis.api.routes.incidents import action_view
from aegis.clients.controlplane import ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.db import audit
from aegis.db.models import ActionRecord, AuditLog, SimulationRecord
from aegis.domain.enums import ActionType
from aegis.domain.schemas import ActionParams
from aegis.security.auth import Permission
from aegis.security.signing import sign_mode_change

router = APIRouter(prefix="/api/v1", tags=["operations"])
READ = Depends(require(Permission.READ))


async def _cp(call: Any) -> Any:
    try:
        return await call
    except ControlPlaneError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc
    except UpstreamUnavailable as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc


@router.get("/actions", tags=["actions"])
async def list_actions(incident: str | None = None, phase: str | None = None, limit: int = Query(100, ge=1, le=500),
                       _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    q = select(ActionRecord).order_by(ActionRecord.created_at.desc()).limit(limit)
    if incident:
        q = q.where(ActionRecord.incident_id == incident)
    if phase:
        q = q.where(ActionRecord.phase == phase)
    return {"items": [action_view(a) for a in (await s.execute(q)).scalars()]}


@router.get("/actions/{action_id}", tags=["actions"])
async def get_action(action_id: str, _: Principal = READ, st: AppState = Depends(state),
                     s: AsyncSession = Depends(session)) -> dict[str, Any]:
    row = await s.get(ActionRecord, action_id)
    if row is None:
        raise APIError(404, "not_found", "action not found")
    live = None
    try:
        live = await st.cp.action(action_id)
    except (ControlPlaneError, UpstreamUnavailable):
        live = None
    return {**action_view(row), "controller": live}


@router.get("/simulations", tags=["simulations"])
async def list_simulations(incident: str | None = None, _: Principal = READ,
                           s: AsyncSession = Depends(session)) -> dict[str, Any]:
    q = select(SimulationRecord).order_by(SimulationRecord.created_at.desc()).limit(100)
    if incident:
        q = q.where(SimulationRecord.incident_id == incident)
    return {"items": [{"id": x.id, "incident_id": x.incident_id, "action_type": x.action_type, "target": x.target,
                       "phase": x.phase, "verdict": x.verdict, "baseline": x.baseline, "candidate": x.candidate,
                       "summary": x.summary, "reason": x.reason, "created_at": x.created_at.isoformat(),
                       "completed_at": x.completed_at.isoformat() if x.completed_at else None}
                      for x in (await s.execute(q)).scalars()]}


@router.get("/policy", tags=["policy"])
async def get_policy(_: Principal = READ, st: AppState = Depends(state)) -> dict[str, Any]:
    return await _cp(st.cp.policy())


class PolicyCheck(BaseModel):
    action_type: str = Field(max_length=40)
    target: str = Field(max_length=253)
    target_kind: Literal["Deployment", "Pod", "CanaryRelease"] = "Deployment"
    parameters: dict[str, Any] = Field(default_factory=dict)
    diagnosis_confidence: int = Field(default=80, ge=0, le=100)


@router.post("/policy/check", tags=["policy"])
async def check_policy(body: PolicyCheck, _: Principal = READ, st: AppState = Depends(state)) -> dict[str, Any]:
    params = ActionParams.model_validate(body.parameters).wire()
    req = {"incidentId": "DRY-RUN", "actionType": body.action_type,
           "target": {"kind": body.target_kind, "namespace": st.settings.managed_namespace, "name": body.target},
           "parameters": params, "diagnosisConfidence": body.diagnosis_confidence}
    return await _cp(st.cp.evaluate(req))


class ModeChange(BaseModel):
    mode: Literal["autonomous", "supervised", "observe"]


@router.put("/policy/mode", tags=["policy"])
async def set_mode(body: ModeChange, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                   st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    signed = sign_mode_change(st.settings.approval_key.get_secret_value().encode(), body.mode, p.username)
    res = await _cp(st.cp.set_mode(signed))
    await audit.append(s, actor=p.username, action="policy.mode", resource="aegispolicy/default", outcome="changed",
                       details=res)
    return res


@router.get("/policy/decisions", tags=["policy"])
async def decisions(limit: int = Query(50, ge=1, le=200), _: Principal = READ,
                    s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = (await s.execute(select(ActionRecord).order_by(ActionRecord.created_at.desc()).limit(limit))).scalars().all()
    out = []
    for a in rows:
        d = a.decision or {}
        out.append({"action_id": a.id, "incident_id": a.incident_id, "action_type": a.action_type,
                    "target": a.target.get("name"), "phase": a.phase, "allowed": d.get("allowed"),
                    "requires_approval": d.get("requiresApproval"), "risk_level": d.get("riskLevel"),
                    "risk_score": d.get("riskScore"), "reasons": d.get("reasons", []), "checks": d.get("checks", []),
                    "created_at": a.created_at.isoformat()})
    return {"items": out, "registry": [t.value for t in ActionType]}


@router.get("/audit", tags=["audit"])
async def audit_log(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), source: str | None = None,
                    _: Principal = Depends(require(Permission.AUDIT)), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
    if source:
        q = q.where(AuditLog.source == source)
    return {"items": [{"id": r.id, "ts": r.ts.isoformat(), "actor": r.actor, "action": r.action, "resource": r.resource,
                       "outcome": r.outcome, "details": r.details, "source": r.source, "hash": r.hash,
                       "prev_hash": r.prev_hash} for r in (await s.execute(q)).scalars()]}


@router.get("/audit/verify", tags=["audit"])
async def audit_verify(_: Principal = Depends(require(Permission.AUDIT)), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    return await audit.verify_chain(s)
