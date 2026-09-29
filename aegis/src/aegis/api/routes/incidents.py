from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, Principal, require, session
from aegis.db import audit
from aegis.db.models import (
    ActionRecord,
    ApprovalRecord,
    Command,
    DiagnosisRecord,
    Event,
    EvidenceRecord,
    Incident,
    PlanRecord,
    Postmortem,
    SimulationRecord,
    TelemetrySnapshot,
    VerificationRecord,
)
from aegis.domain.enums import IncidentStatus
from aegis.security.auth import Permission

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])
READ = Depends(require(Permission.READ))


def incident_summary(i: Incident) -> dict[str, Any]:
    return {"id": i.id, "title": i.title, "severity": i.severity, "status": i.status,
            "detected_at": i.detected_at.isoformat(), "onset_at": i.onset_at.isoformat() if i.onset_at else None,
            "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None, "affected_services": i.affected_services,
            "root_service": i.root_service, "category": i.category, "confidence": i.confidence, "outcome": i.outcome,
            "summary": i.summary, "iteration": i.iteration, "human_interventions": i.human_interventions,
            "duration_seconds": ((i.resolved_at or i.updated_at) - (i.onset_at or i.detected_at)).total_seconds()}


def action_view(a: ActionRecord) -> dict[str, Any]:
    return {"id": a.id, "incident_id": a.incident_id, "action_type": a.action_type, "target": a.target, "params": a.params,
            "phase": a.phase, "risk_level": a.risk_level, "risk_score": a.risk_score, "requires_approval": a.requires_approval,
            "decision": a.decision, "message": a.message, "revert_of": a.revert_of, "simulation_id": a.simulation_id,
            "rationale": a.rationale, "outcome": a.outcome, "category": a.category, "created_at": a.created_at.isoformat(),
            "started_at": a.started_at.isoformat() if a.started_at else None,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None}


async def get_or_404(s: AsyncSession, incident_id: str) -> Incident:
    inc = await s.get(Incident, incident_id)
    if inc is None:
        raise APIError(404, "not_found", f"incident {incident_id} not found")
    return inc


@router.get("")
async def list_incidents(status: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    q = select(Incident).order_by(Incident.detected_at.desc())
    if status == "active":
        q = q.where(Incident.status.not_in([IncidentStatus.RESOLVED.value, IncidentStatus.ESCALATED.value]))
    elif status:
        q = q.where(Incident.status == status.upper())
    total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await s.execute(q.limit(limit).offset(offset))).scalars().all()
    return {"items": [incident_summary(i) for i in rows], "total": total}


@router.get("/{incident_id}")
async def get_incident(incident_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    inc = await get_or_404(s, incident_id)
    dx = (await s.execute(select(DiagnosisRecord).where(DiagnosisRecord.incident_id == incident_id)
                          .order_by(DiagnosisRecord.created_at.desc()).limit(1))).scalar_one_or_none()
    plan = (await s.execute(select(PlanRecord).where(PlanRecord.incident_id == incident_id)
                            .order_by(PlanRecord.created_at.desc()).limit(1))).scalar_one_or_none()
    actions = (await s.execute(select(ActionRecord).where(ActionRecord.incident_id == incident_id)
                               .order_by(ActionRecord.created_at))).scalars().all()
    approvals = (await s.execute(select(ApprovalRecord).where(ApprovalRecord.incident_id == incident_id))).scalars().all()
    sims = (await s.execute(select(SimulationRecord).where(SimulationRecord.incident_id == incident_id)
                            .order_by(SimulationRecord.created_at))).scalars().all()
    vers = (await s.execute(select(VerificationRecord).where(VerificationRecord.incident_id == incident_id)
                            .order_by(VerificationRecord.created_at))).scalars().all()
    pm = await s.get(Postmortem, incident_id)
    ev_count = (await s.execute(select(func.count()).where(EvidenceRecord.incident_id == incident_id))).scalar_one()
    return {
        **incident_summary(inc), "symptoms": inc.symptoms,
        "diagnosis": dx.content if dx else None, "plan": plan.content if plan else None,
        "actions": [action_view(a) for a in actions],
        "approvals": [{"id": a.id, "status": a.status, "requested_at": a.requested_at.isoformat(),
                       "decided_at": a.decided_at.isoformat() if a.decided_at else None, "decided_by": a.decided_by,
                       "reason": a.reason, "context": a.context} for a in approvals],
        "simulations": [{"id": x.id, "action_type": x.action_type, "target": x.target, "phase": x.phase, "verdict": x.verdict,
                         "baseline": x.baseline, "candidate": x.candidate, "summary": x.summary, "reason": x.reason,
                         "created_at": x.created_at.isoformat()} for x in sims],
        "verifications": [v.content for v in vers], "has_postmortem": pm is not None, "evidence_count": ev_count,
    }


@router.get("/{incident_id}/timeline")
async def timeline(incident_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    await get_or_404(s, incident_id)
    rows = (await s.execute(select(Event).where(Event.incident_id == incident_id).order_by(Event.id))).scalars().all()
    return {"items": [{"id": e.id, "ts": e.ts.isoformat(), "type": e.type, "message": e.message, "actor": e.actor,
                       "data": e.data} for e in rows]}


@router.get("/{incident_id}/evidence")
async def evidence(incident_id: str, kind: str | None = None, _: Principal = READ,
                   s: AsyncSession = Depends(session)) -> dict[str, Any]:
    await get_or_404(s, incident_id)
    q = select(EvidenceRecord).where(EvidenceRecord.incident_id == incident_id).order_by(EvidenceRecord.score.desc())
    if kind:
        q = q.where(EvidenceRecord.kind == kind)
    rows = (await s.execute(q)).scalars().all()
    return {"items": [{"id": e.id, "kind": e.kind, "service": e.service, "signal": e.signal, "title": e.title,
                       "summary": e.summary, "data": e.data, "source": e.source, "observed_at": e.observed_at.isoformat(),
                       "score": e.score, "iteration": e.iteration} for e in rows]}


@router.get("/{incident_id}/diagnoses")
async def diagnoses(incident_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = (await s.execute(select(DiagnosisRecord).where(DiagnosisRecord.incident_id == incident_id)
                            .order_by(DiagnosisRecord.created_at))).scalars().all()
    return {"items": [r.content for r in rows]}


@router.get("/{incident_id}/plans")
async def plans(incident_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = (await s.execute(select(PlanRecord).where(PlanRecord.incident_id == incident_id)
                            .order_by(PlanRecord.created_at))).scalars().all()
    return {"items": [r.content for r in rows]}


@router.get("/{incident_id}/snapshots")
async def snapshots(incident_id: str, _: Principal = READ, s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = (await s.execute(select(TelemetrySnapshot).where(TelemetrySnapshot.incident_id == incident_id)
                            .order_by(TelemetrySnapshot.captured_at))).scalars().all()
    return {"items": [{"label": r.label, "captured_at": r.captured_at.isoformat(), "data": r.data} for r in rows]}


@router.get("/{incident_id}/postmortem", response_model=None)
async def postmortem(incident_id: str, format: Literal["json", "markdown"] = "json", _: Principal = READ,
                     s: AsyncSession = Depends(session)) -> dict[str, Any] | PlainTextResponse:
    pm = await s.get(Postmortem, incident_id)
    if pm is None:
        raise APIError(404, "not_found", "postmortem not generated yet (generated when the incident closes)")
    if format == "markdown":
        return PlainTextResponse(pm.markdown, media_type="text/markdown")
    return {"incident_id": incident_id, "generated_at": pm.generated_at.isoformat(), "method": pm.method,
            "content": pm.content, "markdown": pm.markdown}


class OperatorNote(BaseModel):
    note: str = Field(default="", max_length=1000)


async def _command(s: AsyncSession, kind: str, incident_id: str, user: str, payload: dict[str, Any]) -> dict[str, Any]:
    await get_or_404(s, incident_id)
    cmd = Command(type=kind, incident_id=incident_id, payload=payload, created_by=user)
    s.add(cmd)
    await s.flush()
    await audit.append(s, actor=user, action=f"incident.{kind}", resource=f"incident/{incident_id}", outcome="requested",
                       details=payload)
    return {"command_id": cmd.id, "type": kind, "incident_id": incident_id, "status": "queued"}


@router.post("/{incident_id}/investigate", status_code=202)
async def investigate(incident_id: str, p: Principal = Depends(require(Permission.INVESTIGATE)),
                      s: AsyncSession = Depends(session)) -> dict[str, Any]:
    return await _command(s, "investigate", incident_id, p.username, {})


@router.post("/{incident_id}/resolve", status_code=202)
async def resolve(incident_id: str, body: OperatorNote, p: Principal = Depends(require(Permission.RESOLVE)),
                  s: AsyncSession = Depends(session)) -> dict[str, Any]:
    return await _command(s, "resolve", incident_id, p.username, {"note": body.note})


@router.post("/{incident_id}/escalate", status_code=202)
async def escalate(incident_id: str, body: OperatorNote, p: Principal = Depends(require(Permission.RESOLVE)),
                   s: AsyncSession = Depends(session)) -> dict[str, Any]:
    return await _command(s, "escalate", incident_id, p.username, {"note": body.note})
