"""Closed vocabularies shared by the engine, API, evaluation and model contracts."""

from __future__ import annotations

from enum import StrEnum


class IncidentStatus(StrEnum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ROLLED_BACK = "ROLLED_BACK"
    ESCALATED = "ESCALATED"

    @property
    def terminal(self) -> bool:
        return self in (IncidentStatus.RESOLVED, IncidentStatus.ESCALATED)


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"


class CauseCategory(StrEnum):
    BAD_DEPLOYMENT = "BAD_DEPLOYMENT"
    CONFIG_ERROR = "CONFIG_ERROR"
    RESOURCE_MISCONFIGURATION = "RESOURCE_MISCONFIGURATION"
    CPU_SATURATION = "CPU_SATURATION"
    MEMORY_EXHAUSTION = "MEMORY_EXHAUSTION"
    DB_CONNECTION_EXHAUSTION = "DB_CONNECTION_EXHAUSTION"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    NETWORK_DEGRADATION = "NETWORK_DEGRADATION"
    POD_CRASHLOOP = "POD_CRASHLOOP"
    UNKNOWN = "UNKNOWN"


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    K8S_STATE = "k8s_state"
    K8S_EVENT = "k8s_event"
    CHANGE = "change"
    TOPOLOGY = "topology"
    RUNBOOK = "runbook"
    HISTORY = "history"
    CANARY = "canary"


class ActionType(StrEnum):
    """Mirror of the control plane's action registry (the controller is authoritative)."""

    RESTART_POD = "restart_pod"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DEPLOYMENT = "scale_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    PATCH_RESOURCES = "patch_resources"
    PAUSE_ROLLOUT = "pause_rollout"
    RESUME_ROLLOUT = "resume_rollout"
    UPDATE_CONFIG = "update_config"
    ISOLATE_POD = "isolate_pod"
    APPLY_NETWORK_POLICY = "apply_network_policy"
    ABORT_CANARY = "abort_canary"


class VerificationOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    NO_EFFECT = "NO_EFFECT"
    DEGRADED = "DEGRADED"


class ClaimType(StrEnum):
    """Epistemic status of a statement in reports."""

    FACT = "FACT"
    HYPOTHESIS = "HYPOTHESIS"
    INTERPRETATION = "MODEL_INTERPRETATION"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"
