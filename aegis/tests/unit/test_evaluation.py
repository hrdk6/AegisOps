"""Benchmark scoring and statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis.chaos.scenarios import load_scenarios
from aegis.evaluation.report import markdown, summarize
from aegis.evaluation.scoring import Observation, score
from aegis.evaluation.stats import bootstrap_mean, brier, wilson
from tests.conftest import REPO

SCEN = load_scenarios(Path(REPO) / "benchmarks" / "scenarios")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def incident(outcome: str, status: str, actions: list[tuple[str, str, str]], category: str, component: str,
             approvals: int = 0) -> dict:
    return {"id": "INC-1", "status": status, "outcome": outcome, "detected_at": (T0 + timedelta(seconds=30)).isoformat(),
            "resolved_at": (T0 + timedelta(seconds=150)).isoformat() if status == "RESOLVED" else None,
            "human_interventions": approvals,
            "diagnosis": {"confidence": 0.9, "method": "rules",
                          "selected": {"category": category, "component": component, "supporting_evidence": ["ev-1"]},
                          "hypotheses": [{"category": category, "component": component}]},
            "actions": [{"action_type": a, "target": {"name": t}, "phase": p} for a, t, p in actions]}


def test_correct_autonomous_resolution_passes() -> None:
    inc = incident("resolved_autonomously", "RESOLVED", [("rollback_deployment", "payment-service", "Succeeded")],
                   "BAD_DEPLOYMENT", "payment-service")
    m = score(Observation(SCEN["payment-bad-release"], 1, T0, T0 + timedelta(minutes=3), inc, {"ev-1"}, {"ev-1": "change"}))
    assert m["passed"] and m["root_cause_correct"] and m["first_action_optimal"] and m["remediation_success"]
    assert m["time_to_detect_s"] == 30 and m["recovery_time_s"] == 150 and m["evidence_grounding"] == 1.0


def test_prohibited_action_is_unsafe_even_if_resolved() -> None:
    inc = incident("resolved_autonomously", "RESOLVED", [("rollout_restart", "postgres", "Succeeded")],
                   "BAD_DEPLOYMENT", "payment-service")
    m = score(Observation(SCEN["payment-bad-release"], 1, T0, T0, inc, {"ev-1"}))
    assert m["unsafe_action"] and m["false_remediation"] and not m["passed"]


def test_network_scenario_requires_escalation_without_actions() -> None:
    good = incident("escalated", "ESCALATED", [], "NETWORK_DEGRADATION", "payment-service")
    m = score(Observation(SCEN["order-payment-network-latency"], 1, T0, T0, good, {"ev-1"}))
    assert m["passed"]
    bad = incident("resolved_autonomously", "RESOLVED", [("rollback_deployment", "payment-service", "Succeeded")],
                   "BAD_DEPLOYMENT", "payment-service")
    m2 = score(Observation(SCEN["order-payment-network-latency"], 1, T0, T0, bad, {"ev-1"}))
    assert not m2["passed"] and m2["false_remediation"] and not m2["root_cause_correct"]


def test_transient_scenario_accepts_no_incident() -> None:
    m = score(Observation(SCEN["payment-pod-kill"], 1, T0, T0, None))
    assert m["passed"] and m["outcome"] == "no_incident"


def test_wilson_and_bootstrap() -> None:
    lo, hi = wilson(18, 20)
    assert 0.68 < lo < 0.9 < hi <= 0.99
    assert wilson(0, 0) == (0.0, 1.0)
    mean, blo, bhi = bootstrap_mean([10.0, 12.0, 14.0, 16.0])
    assert mean == 13.0 and blo <= mean <= bhi
    assert brier([(1.0, True), (0.0, False)]) == 0.0


def test_summary_and_report_render() -> None:
    inc = incident("resolved_autonomously", "RESOLVED", [("rollback_deployment", "payment-service", "Succeeded")],
                   "BAD_DEPLOYMENT", "payment-service")
    results = [score(Observation(SCEN["payment-bad-release"], r, T0, T0, inc, {"ev-1"})) for r in (1, 2)]
    summary = summarize(results)
    assert summary["root_cause_accuracy"]["value"] == 1.0
    md = markdown({"run_id": "x", "mode": "live"}, summary, results)
    assert "Root-cause accuracy" in md and "payment-bad-release" in md


@pytest.mark.parametrize("sid", sorted(SCEN))
def test_every_scenario_scores_without_error(sid: str) -> None:
    score(Observation(SCEN[sid], 1, T0, T0, None))
