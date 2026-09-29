"""SQLAlchemy ORM models for AegisOps state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = now_col()


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    onset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    affected_services: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    symptoms: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    root_service: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String(40))
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    human_interventions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Event(Base):
    """Timeline and system events (streamed to clients via LISTEN/NOTIFY)."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    type: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="aegis-engine")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))
    service: Mapped[str | None] = mapped_column(String(64))
    signal: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    score: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = now_col()


class DiagnosisRecord(Base):
    __tablename__ = "diagnoses"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    iteration: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = now_col()


class PlanRecord(Base):
    __tablename__ = "remediation_plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    diagnosis_id: Mapped[str] = mapped_column(String(40))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = now_col()


class ActionRecord(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # controller RemediationAction name
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(40))
    candidate_id: Mapped[str | None] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(32))
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    category: Mapped[str | None] = mapped_column(String(40))
    phase: Mapped[str] = mapped_column(String(24))
    risk_level: Mapped[str | None] = mapped_column(String(12))
    risk_score: Mapped[int | None] = mapped_column(Integer)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    message: Mapped[str] = mapped_column(Text, default="")
    revert_of: Mapped[str | None] = mapped_column(String(80))
    simulation_id: Mapped[str | None] = mapped_column(String(80))
    rationale: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String(24))  # verification outcome
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # == action id
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending|approved|rejected|expired|superseded
    requested_at: Mapped[datetime] = now_col()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    spec_hash: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class SimulationRecord(Base):
    __tablename__ = "simulations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(32))
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)
    phase: Mapped[str] = mapped_column(String(24))
    verdict: Mapped[str | None] = mapped_column(String(24))
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    candidate: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = now_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationRecord(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    action_id: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = now_col()


class TelemetrySnapshot(Base):
    __tablename__ = "telemetry_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = now_col()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Postmortem(Base):
    __tablename__ = "postmortems"
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True)
    generated_at: Mapped[datetime] = now_col()
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    markdown: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(24))


class AuditLog(Base):
    """Append-only, hash-chained audit trail (UPDATE/DELETE blocked by trigger)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(24), default="aegis")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now_col()


class Runbook(Base):
    __tablename__ = "runbooks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    categories: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    body: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    __table_args__ = (Index("ix_runbooks_tsv", "tsv", postgresql_using="gin"),)


class ServiceStatus(Base):
    __tablename__ = "service_status"
    service: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ComponentHeartbeat(Base):
    __tablename__ = "component_heartbeats"
    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Command(Base):
    """Operator commands from the API to the engine (LISTEN/NOTIFY bus)."""

    __tablename__ = "commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32))
    incident_id: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = now_col()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))  # live | replay
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    repetition: Mapped[int] = mapped_column(Integer)
    incident_id: Mapped[str | None] = mapped_column(String(40))
    passed: Mapped[bool] = mapped_column(Boolean)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
