"""Typed domain objects exchanged between engine stages, persisted as JSON and
served by the API."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.enums import ActionType, CauseCategory, ClaimType, EvidenceKind, VerificationOutcome


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# ---------------------------------------------------------------- telemetry --


class Anomaly(Model):
    signal: str
    value: float
    baseline: float | None = None
    threshold: float | None = None
    reason: str
    since: datetime


class ServiceSignals(Model):
    """One detector evaluation for one workload."""

    service: str
    at: datetime
    rps: float | None = None
    rps_baseline: float | None = None
    error_ratio: float | None = None
    p95_ms: float | None = None
    p95_baseline: float | None = None
    cpu_util: float | None = None
    mem_util: float | None = None
    throttle_ratio: float | None = None
    db_pool_util: float | None = None
    db_waiting: float | None = None
    db_timeouts_per_s: float | None = None
    restarts_recent: int = 0
    oom_recent: int = 0
    crashloop_pods: int = 0
    ready: int = 0
    desired: int = 0
    status: Literal["healthy", "degraded", "down", "unknown"] = "unknown"
    anomalies: list[Anomaly] = Field(default_factory=list)
    # Candidate anomalies not yet confirmed by persistence (context for investigations, never paged on).
    pending: list[Anomaly] = Field(default_factory=list)

    def anomaly(self, signal: str) -> Anomaly | None:
        return next((a for a in self.anomalies if a.signal == signal), None)


# ----------------------------------------------------------------- evidence --


class EvidenceSource(Model):
    system: Literal["prometheus", "loki", "jaeger", "kubernetes", "controlplane", "knowledge", "aegisops"]
    query: str | None = None
    url: str | None = None
    start: datetime | None = None
    end: datetime | None = None


class Evidence(Model):
    id: str
    kind: EvidenceKind
    service: str | None = None
    signal: str | None = None
    title: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    source: EvidenceSource
    observed_at: datetime
    score: float = 0.5

    @staticmethod
    def make_id(kind: str, service: str | None, signal: str | None, discriminator: str = "") -> str:
        raw = f"{kind}|{service}|{signal}|{discriminator}"
        return "ev-" + hashlib.sha256(raw.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- diagnosis --


class Hypothesis(Model):
    category: CauseCategory
    component: str
    statement: str
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    trigger_change: str | None = None  # evidence id of the correlated change
    edge: str | None = None  # "caller->dependency" for network hypotheses
    rule_score: float = 0.0
    source: Literal["rules", "model", "rules+model"] = "rules"


class Diagnosis(Model):
    id: str
    incident_id: str
    iteration: int
    summary: str
    hypotheses: list[Hypothesis]
    selected: Hypothesis | None
    confidence: float
    affected_services: list[str]
    blast_radius: list[str]
    uncertainty: str
    method: Literal["rules", "rules+model"]
    model_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# -------------------------------------------------------------- remediation --


class ActionParams(Model):
    replicas: int | None = Field(default=None, ge=0, le=50)
    toRevision: int | None = Field(default=None, ge=1)  # noqa: N815 (wire format)
    container: str | None = None
    resources: dict[str, str] | None = None
    configMap: str | None = None  # noqa: N815
    configRevision: str | None = None  # noqa: N815
    networkPolicyTemplate: Literal["deny-all-ingress", "allow-same-namespace-only"] | None = None  # noqa: N815

    def wire(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class Target(Model):
    kind: Literal["Deployment", "Pod", "CanaryRelease"] = "Deployment"
    namespace: str
    name: str


class RemediationCandidate(Model):
    id: str
    action_type: ActionType
    target: Target
    params: ActionParams = Field(default_factory=ActionParams)
    rationale: str
    expected_effect: str
    affected_components: list[str]
    rollback_strategy: str
    prerequisites: list[str]
    efficacy: float = Field(ge=0.0, le=1.0)  # P(success | hypothesis true)
    confidence: float = Field(ge=0.0, le=1.0)  # efficacy * hypothesis confidence
    addresses: CauseCategory
    source: Literal["playbook", "model"] = "playbook"
    simulatable: bool = False
    # Rollout revision of the target Deployment when the plan was made; sent as a
    # precondition so the controller refuses the action if the target has changed since.
    expected_revision: int | None = None
    # Filled by the policy dry run (authoritative values come from the controller).
    policy_allowed: bool | None = None
    requires_approval: bool | None = None
    risk_level: str | None = None
    risk_score: int | None = None
    policy_reasons: list[str] = Field(default_factory=list)
    utility: float = 0.0

    def fingerprint(self) -> str:
        """Identity for "already attempted in this incident". A rollback is the same remedy whatever
        revision number it resolves to (the numbers shift after every rollout, including our own)."""
        params = self.params.wire()
        if self.action_type == ActionType.ROLLBACK_DEPLOYMENT:
            params.pop("toRevision", None)
        return json.dumps([self.action_type, self.target.model_dump(), params], sort_keys=True)


class RemediationPlan(Model):
    id: str
    incident_id: str
    diagnosis_id: str
    candidates: list[RemediationCandidate]
    selected_index: int | None
    escalation_reason: str | None = None
    created_at: datetime


# ------------------------------------------------------------- verification --


class VerificationCheck(Model):
    name: str
    service: str | None = None
    passed: bool
    before: float | None = None
    after: float | None = None
    threshold: float | None = None
    detail: str = ""


class Verification(Model):
    id: str
    incident_id: str
    action_id: str
    outcome: VerificationOutcome
    checks: list[VerificationCheck]
    summary: str
    started_at: datetime
    completed_at: datetime


# ---------------------------------------------------------------- reporting --


class Claim(Model):
    type: ClaimType
    text: str
    evidence: list[str] = Field(default_factory=list)


class PostmortemSection(Model):
    title: str
    claims: list[Claim]
