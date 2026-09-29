"""Persistence helpers used by the engine and the API."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    ActionRecord,
    ApprovalRecord,
    ComponentHeartbeat,
    DiagnosisRecord,
    Event,
    EvidenceRecord,
    Incident,
    PlanRecord,
    ServiceStatus,
    TelemetrySnapshot,
)
from aegis.domain.enums import IncidentStatus
from aegis.domain.schemas import Diagnosis, Evidence, RemediationPlan
from aegis.security.redaction import redact, redact_obj


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_incident_id(now: datetime | None = None) -> str:
    now = now or utcnow()
    return f"INC-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"


async def add_event(session: AsyncSession, incident_id: str | None, type_: str, message: str,
                    actor: str = "aegis-engine", data: dict[str, Any] | None = None) -> Event:
    ev = Event(incident_id=incident_id, type=type_, message=redact(message)[:2000], actor=actor,
               data=redact_obj(data or {}), ts=utcnow())
    session.add(ev)
    await session.flush()
    return ev


async def get_incident(session: AsyncSession, incident_id: str) -> Incident | None:
    return await session.get(Incident, incident_id)


async def set_status(session: AsyncSession, incident: Incident, status: IncidentStatus, message: str,
                     data: dict[str, Any] | None = None, actor: str = "aegis-engine") -> None:
    previous = incident.status
    incident.status = status.value
    incident.updated_at = utcnow()
    if status == IncidentStatus.RESOLVED and incident.resolved_at is None:
        incident.resolved_at = utcnow()
    await add_event(session, incident.id, "status_changed", message,
                    actor=actor, data={"from": previous, "to": status.value, **(data or {})})


async def open_incidents(session: AsyncSession) -> list[Incident]:
    terminal = [IncidentStatus.RESOLVED.value, IncidentStatus.ESCALATED.value]
    q = select(Incident).where(Incident.status.not_in(terminal)).order_by(Incident.detected_at.desc())
    return list((await session.execute(q)).scalars())


async def close_pending_approvals(session: AsyncSession, status: str, incident_id: str | None = None) -> list[str]:
    """Close approvals nobody can act on any more: those of `incident_id`, or (when None) of every
    closed incident. The controller independently expires the underlying action after its TTL."""
    q = select(ApprovalRecord).where(ApprovalRecord.status == "pending")
    if incident_id is not None:
        q = q.where(ApprovalRecord.incident_id == incident_id)
    else:
        closed = select(Incident.id).where(Incident.status.in_([IncidentStatus.RESOLVED.value, IncidentStatus.ESCALATED.value]))
        q = q.where(ApprovalRecord.incident_id.in_(closed))
    closed_ids = []
    for ap in (await session.execute(q)).scalars():
        ap.status, ap.decided_at = status, utcnow()
        closed_ids.append(ap.id)
        await add_event(session, ap.incident_id, "approval_decided", f"Approval for {ap.id}: {status} (no longer actionable)")
    return closed_ids


async def escalated_holding(session: AsyncSession, services: list[str], since: datetime) -> Incident | None:
    """Most recent unresolved escalated incident that overlaps `services` and was still symptomatic after `since`."""
    q = (select(Incident).where(Incident.status == IncidentStatus.ESCALATED.value, Incident.resolved_at.is_(None),
                                Incident.updated_at >= since)
         .order_by(Incident.detected_at.desc()))
    for inc in (await session.execute(q)).scalars():
        if set(inc.affected_services) & set(services):
            return inc
    return None


async def save_evidence(session: AsyncSession, incident_id: str, iteration: int, items: list[Evidence]) -> None:
    for ev in items:
        stmt = pg_insert(EvidenceRecord).values(
            incident_id=incident_id, id=ev.id, iteration=iteration, kind=ev.kind.value, service=ev.service,
            signal=ev.signal, title=redact(ev.title)[:300], summary=redact(ev.summary), data=redact_obj(ev.data),
            source=ev.source.model_dump(mode="json"), observed_at=ev.observed_at, score=ev.score,
        ).on_conflict_do_update(
            index_elements=["incident_id", "id"],
            set_={"iteration": iteration, "summary": redact(ev.summary), "data": redact_obj(ev.data),
                  "score": ev.score, "observed_at": ev.observed_at},
        )
        await session.execute(stmt)


async def save_diagnosis(session: AsyncSession, d: Diagnosis) -> None:
    session.add(DiagnosisRecord(id=d.id, incident_id=d.incident_id, iteration=d.iteration,
                                content=d.model_dump(mode="json"), confidence=d.confidence, method=d.method))
    await session.flush()


async def save_plan(session: AsyncSession, p: RemediationPlan) -> None:
    session.add(PlanRecord(id=p.id, incident_id=p.incident_id, diagnosis_id=p.diagnosis_id,
                           content=p.model_dump(mode="json")))
    await session.flush()


async def snapshot(session: AsyncSession, incident_id: str, label: str, data: dict[str, Any]) -> None:
    session.add(TelemetrySnapshot(incident_id=incident_id, label=label, data=data, captured_at=utcnow()))
    await session.flush()


async def upsert_action(session: AsyncSession, action_id: str, **values: Any) -> None:
    values = {k: v for k, v in values.items() if v is not None}
    stmt = pg_insert(ActionRecord).values(id=action_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={**values, "updated_at": utcnow()})
    await session.execute(stmt)


async def update_action(session: AsyncSession, action_id: str, **values: Any) -> None:
    """Partial update of an existing action row (never inserts)."""
    await session.execute(update(ActionRecord).where(ActionRecord.id == action_id).values(**values, updated_at=utcnow()))


async def upsert_service_status(session: AsyncSession, service: str, status: str, signals: dict[str, Any]) -> None:
    stmt = pg_insert(ServiceStatus).values(service=service, status=status, signals=signals, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(index_elements=["service"],
                                      set_={"status": status, "signals": signals, "updated_at": utcnow()})
    await session.execute(stmt)


async def heartbeat(session: AsyncSession, name: str, status: str, details: dict[str, Any] | None = None) -> None:
    stmt = pg_insert(ComponentHeartbeat).values(name=name, status=status, details=details or {}, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(index_elements=["name"],
                                      set_={"status": status, "details": details or {}, "updated_at": utcnow()})
    await session.execute(stmt)


async def bump_interventions(session: AsyncSession, incident_id: str) -> None:
    await session.execute(update(Incident).where(Incident.id == incident_id)
                          .values(human_interventions=Incident.human_interventions + 1))
