"""Human approval workflow.

The API authenticates the human, checks the APPROVE permission, signs the
decision with the approval key (bound to the action's spec hash) and forwards
it to the controller, which verifies the signature before acting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, AppState, Principal, require, session, state
from aegis.clients.controlplane import ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.db import audit, repo
from aegis.db.models import ApprovalRecord, Incident
from aegis.security.auth import Permission
from aegis.security.signing import sign_decision

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


def view(a: ApprovalRecord, inc: Incident | None = None) -> dict[str, Any]:
    return {"id": a.id, "incident_id": a.incident_id, "incident_title": inc.title if inc else None,
            "severity": inc.severity if inc else None, "status": a.status, "requested_at": a.requested_at.isoformat(),
            "decided_at": a.decided_at.isoformat() if a.decided_at else None, "decided_by": a.decided_by,
            "reason": a.reason, "context": a.context}


@router.get("")
async def list_approvals(status: str | None = None, _: Principal = Depends(require(Permission.READ)),
                         s: AsyncSession = Depends(session)) -> dict[str, Any]:
    q = select(ApprovalRecord, Incident).join(Incident, Incident.id == ApprovalRecord.incident_id)\
        .order_by(ApprovalRecord.requested_at.desc()).limit(100)
    if status:
        q = q.where(ApprovalRecord.status == status)
    return {"items": [view(a, i) for a, i in (await s.execute(q)).all()]}


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=500)


@router.post("/{action_id}/decision")
async def decide(action_id: str, body: Decision, p: Principal = Depends(require(Permission.APPROVE)),
                 st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    appr = await s.get(ApprovalRecord, action_id)
    if appr is None:
        raise APIError(404, "not_found", "approval request not found")
    if appr.status != "pending":
        raise APIError(409, "already_decided", f"approval is {appr.status}")
    try:
        action = await st.cp.action(action_id)
    except (ControlPlaneError, UpstreamUnavailable) as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc
    if action["phase"] != "AwaitingApproval":
        raise APIError(409, "invalid_state", f"action is {action['phase']}, no longer awaiting approval")
    signed = sign_decision(st.settings.approval_key.get_secret_value().encode(), action_id, action["specHash"],
                           body.decision, p.username, body.reason)
    try:
        result = await st.cp.approve(action_id, signed)
    except ControlPlaneError as exc:
        await audit.append(s, actor=p.username, action=f"approval.{body.decision}", resource=f"action/{action_id}",
                           outcome="rejected_by_controlplane", details={"error": exc.message})
        raise APIError(exc.status, exc.code, exc.message) from exc
    except UpstreamUnavailable as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc
    appr.status, appr.decided_by, appr.reason, appr.decided_at = body.decision, p.username, body.reason, datetime.now(UTC)
    await audit.append(s, actor=p.username, action=f"approval.{body.decision}", resource=f"action/{action_id}",
                       outcome="accepted", details={"incident": appr.incident_id, "reason": body.reason})
    await repo.add_event(s, appr.incident_id, "approval_submitted",
                         f"{p.username} {body.decision} {action_id}" + (f": {body.reason}" if body.reason else ""),
                         actor=p.username)
    return {"approval": view(appr), "action": result}
