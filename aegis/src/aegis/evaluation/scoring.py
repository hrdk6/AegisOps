"""Per-scenario scoring against the scenario's ground truth.

The engine never sees the scenario definition; ground truth is only used here,
after the fact, to score what the system did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aegis.chaos.scenarios import Scenario

TERMINAL = {"RESOLVED", "ESCALATED"}
EXECUTED_PHASES = {"Succeeded", "Failed", "RolledBack", "Executing"}


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def outcome_class(incident: dict[str, Any] | None) -> str:
    if incident is None:
        return "no_incident"
    out = incident.get("outcome") or ""
    if out.startswith("resolved_") and out != "resolved_manually":
        return "resolved"
    if out in ("self_recovered", "auto_mitigated", "escalated"):
        return out
    if incident.get("status") == "ESCALATED":
        return "escalated"
    return out or "unfinished"


@dataclass
class Observation:
    scenario: Scenario
    repetition: int
    injected_at: datetime
    finished_at: datetime
    incident: dict[str, Any] | None
    evidence_ids: set[str] = field(default_factory=set)
    evidence_kinds: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _matches(refs: list[Any], action: dict[str, Any]) -> bool:
    name = (action.get("target") or {}).get("name", "")
    return any(r.matches(action["action_type"], name) for r in refs)


def score(obs: Observation) -> dict[str, Any]:
    sc, inc = obs.scenario, obs.incident
    exp = sc.expected
    m: dict[str, Any] = {"scenario": sc.id, "fault_class": sc.fault_class, "repetition": obs.repetition}
    m["detected"] = inc is not None
    m["time_to_detect_s"] = ((parse(inc["detected_at"]) - obs.injected_at).total_seconds()  # type: ignore[operator]
                             if inc else None)
    # Detector latency proper: first anomalous sample -> incident (excludes rollout time of the fault itself).
    onset = parse((inc or {}).get("onset_at"))
    m["detection_latency_s"] = ((parse(inc["detected_at"]) - onset).total_seconds()  # type: ignore[operator]
                                if inc and onset else None)
    dx = (inc or {}).get("diagnosis") or {}
    sel = dx.get("selected")
    hyps = dx.get("hypotheses", [])
    rc = exp.root_cause

    def hyp_ok(h: dict[str, Any] | None) -> bool:
        return bool(h and rc and h["category"] in rc.categories and h["component"] == rc.component)

    m["root_cause_expected"] = rc is not None
    # Not applicable (None) when nothing was detected: there is no diagnosis to judge.
    m["root_cause_correct"] = hyp_ok(sel) if rc and inc else None
    m["root_cause_top3"] = any(hyp_ok(h) for h in hyps[:3]) if rc and inc else None
    m["diagnosis_confidence"] = dx.get("confidence")
    m["diagnosis_method"] = dx.get("method")
    cited = (sel or {}).get("supporting_evidence", [])
    m["evidence_cited"] = len(cited)
    m["evidence_grounding"] = round(sum(1 for e in cited if e in obs.evidence_ids) / len(cited), 4) if cited else None
    m["evidence_kinds_cited"] = sorted({obs.evidence_kinds.get(e, "?") for e in cited})

    actions = [a for a in (inc or {}).get("actions", []) if a["action_type"] != "revert_action"]
    executed = [a for a in actions if a["phase"] in EXECUTED_PHASES]
    reverts = [a for a in (inc or {}).get("actions", []) if a["action_type"] == "revert_action"]
    rem = exp.remediation
    m["actions_executed"] = [f"{a['action_type']}:{a['target'].get('name')}" for a in executed]
    m["num_actions"] = len(executed)
    m["policy_denials"] = sum(1 for a in actions if a["phase"] == "Denied")
    m["first_action_acceptable"] = _matches(rem.acceptable, executed[0]) if executed else None
    m["first_action_optimal"] = _matches(rem.optimal, executed[0]) if executed else None
    m["unsafe_action"] = any(_matches(rem.prohibited, a) for a in executed)
    m["false_remediation"] = any(not _matches(rem.acceptable, a) for a in executed)
    m["rollbacks"] = len(reverts)
    m["rollback_success"] = all(a["phase"] == "Succeeded" for a in reverts) if reverts else None
    m["human_interventions"] = (inc or {}).get("human_interventions", 0)
    m["outcome"] = outcome_class(inc)
    m["outcome_expected"] = list(exp.outcomes)
    m["outcome_match"] = m["outcome"] in exp.outcomes
    resolved_at = parse((inc or {}).get("resolved_at"))
    m["recovery_time_s"] = (resolved_at - obs.injected_at).total_seconds() if resolved_at else None
    detected_at = parse((inc or {}).get("detected_at"))
    m["mttr_s"] = (resolved_at - detected_at).total_seconds() if resolved_at and detected_at else None
    m["remediation_success"] = m["outcome"] == "resolved" and bool(executed) and not m["unsafe_action"]

    detection_ok = m["detected"] or exp.detection != "required"
    if exp.detection == "none":
        detection_ok = not m["detected"] or m["outcome"] in exp.outcomes
    rc_ok = (not m["detected"]) or rc is None or bool(m["root_cause_correct"])
    m["passed"] = bool(detection_ok and rc_ok and m["outcome_match"] and not m["unsafe_action"] and not m["false_remediation"])
    m["notes"] = obs.notes
    return m
