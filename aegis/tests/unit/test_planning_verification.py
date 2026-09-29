"""Remediation planning (fail-closed policy dry run) and verification outcomes."""

from __future__ import annotations

from typing import Any

from aegis.clients.http import UpstreamUnavailable
from aegis.domain.enums import ActionType, CauseCategory, VerificationOutcome
from aegis.engine.diagnosis.engine import DiagnosisEngine
from aegis.engine.planner import Planner
from aegis.engine.slo import SLOBook
from aegis.engine.verification import Verifier, badness, checks_for
from tests.conftest import bundle, change, signals, workloads


class FakeCP:
    def __init__(self, decision: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.decision = decision or {"allowed": True, "requiresApproval": False, "riskLevel": "LOW", "riskScore": 20}
        self.fail = fail
        self.requests: list[dict[str, Any]] = []

    async def evaluate(self, req: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(req)
        if self.fail:
            raise UpstreamUnavailable("controlplane", "down")
        if req["actionType"] == "rollback_deployment":
            return {"decision": {**self.decision, "riskLevel": "MEDIUM", "riskScore": 45, "requiresApproval": True}}
        return {"decision": self.decision}


async def plan_for(b, cp: FakeCP):  # type: ignore[no-untyped-def]
    dx = await DiagnosisEngine().diagnose("INC-T", "t", b, 1)
    return dx, await Planner(cp, "shop").plan(dx, b)  # type: ignore[arg-type]


def bad_release():  # type: ignore[no-untyped-def]
    return bundle({"payment-service": signals("payment-service", ("error_ratio", 0.34))},
                  changes=[change("payment-service", "release", [("containers.p.image", "a", "b")])],
                  workloads=workloads(payment_service=2))


async def test_bad_release_plans_rollback_of_the_culprit() -> None:
    cp = FakeCP()
    _, plan = await plan_for(bad_release(), cp)
    assert plan.selected_index is not None
    sel = plan.candidates[plan.selected_index]
    assert sel.action_type == ActionType.ROLLBACK_DEPLOYMENT and sel.target.name == "payment-service"
    assert sel.simulatable and sel.risk_level == "MEDIUM" and sel.requires_approval
    assert sel.rollback_strategy and sel.expected_effect and sel.prerequisites
    assert all(r["target"]["namespace"] == "shop" for r in cp.requests)
    # The plan is pinned to the observed rollout: explicit target revision plus a
    # precondition that the controller enforces (stale-plan protection).
    assert sel.params.toRevision == 1 and sel.expected_revision == 2


async def test_rollback_fingerprint_ignores_revision_numbers() -> None:
    """After a rollback and its revert the revision numbers move; the remedy must still count as attempted."""
    _, first = await plan_for(bad_release(), FakeCP())
    later = bad_release()
    later.workloads["payment-service"] = workloads(payment_service=4)["payment-service"]
    _, second = await plan_for(later, FakeCP())
    a, b = first.candidates[first.selected_index], second.candidates[second.selected_index]  # type: ignore[index]
    assert a.params.toRevision != b.params.toRevision
    assert a.fingerprint() == b.fingerprint()


async def test_policy_unavailable_fails_closed() -> None:
    _, plan = await plan_for(bad_release(), FakeCP(fail=True))
    assert plan.selected_index is None
    assert all(c.policy_allowed is False for c in plan.candidates)
    assert "denied" in (plan.escalation_reason or "") or "policy" in (plan.escalation_reason or "")


async def test_policy_denial_is_never_selected() -> None:
    _, plan = await plan_for(bad_release(), FakeCP(decision={"allowed": False, "requiresApproval": False,
                                                              "riskLevel": "CRITICAL", "reasons": ["prohibited"]}))
    assert plan.selected_index is None


async def test_network_degradation_escalates_without_action() -> None:
    b = bundle({"order-service": signals("order-service", ("latency_p95", 1100), p95_ms=1100),
                "payment-service": signals("payment-service", p95_ms=20)},
               edge_metrics={"order-service->payment-service": {"p95_ms": 900, "error_ratio": 0.0, "baseline_p95_ms": 25}},
               workloads=workloads())
    dx, plan = await plan_for(b, FakeCP())
    assert dx.selected and dx.selected.category == CauseCategory.NETWORK_DEGRADATION
    assert plan.selected_index is None and "network" in (plan.escalation_reason or "")


async def test_scaled_to_zero_restores_previous_replica_count() -> None:
    b = bundle({"redis": signals("redis", ("scaled_to_zero", 0), ready=0, desired=0, rps=None),
                "auth-service": signals("auth-service", ("error_ratio", 0.4))},
               changes=[change("redis", "scale", [("spec.replicas", "1", "0")])], workloads=workloads())
    b.workloads["redis"]["replicas"] = 0
    _, plan = await plan_for(b, FakeCP())
    sel = plan.candidates[plan.selected_index or 0]
    assert sel.action_type == ActionType.SCALE_DEPLOYMENT and sel.params.replicas == 1 and sel.target.name == "redis"


async def test_resource_restore_uses_previous_values_from_change_log() -> None:
    b = bundle({"order-service": signals("order-service", ("cpu_throttling", 0.9), ("latency_p95", 900), cpu_util=0.95,
                                         throttle_ratio=0.9, p95_ms=900)},
               changes=[change("order-service", "resources", [("containers.order-service.resources.limits.cpu", "500m", "40m"),
                                                              ("containers.order-service.resources.requests.cpu", "50m", "20m")])],
               workloads=workloads(order_service=2))
    _, plan = await plan_for(b, FakeCP())
    patch = next(c for c in plan.candidates if c.action_type == ActionType.PATCH_RESOURCES)
    assert patch.params.resources == {"cpuLimit": "500m", "cpuRequest": "50m"}
    assert patch.params.container == "order-service"


def test_verification_checks_and_badness() -> None:
    slos = SLOBook.from_dict({"defaults": {"max_error_ratio": 0.02, "p95_ms": 300}, "services": {"payment-service": {}}})
    before = signals("payment-service", ("error_ratio", 0.34), error_ratio=0.34)
    after = signals("payment-service", error_ratio=0.0)
    assert badness(before, slos) > badness(after, slos) == 0
    checks = checks_for("payment-service", before, after, slos)
    assert all(c.passed for c in checks)
    failing = signals("payment-service", ("error_ratio", 0.2), error_ratio=0.2)
    assert not all(c.passed for c in checks_for("payment-service", before, failing, slos))


class FakeDetector:
    def __init__(self, snaps: list[dict[str, Any]]) -> None:
        self.snaps = snaps
        self.latest: dict[str, Any] = snaps[0]

    async def next_cycle(self, timeout: float = 30) -> bool:
        if len(self.snaps) > 1:
            self.snaps.pop(0)
        self.latest = self.snaps[0]
        return True


async def _verify(after_snaps: list[dict[str, Any]], before: dict[str, Any]) -> VerificationOutcome:
    slos = SLOBook.from_dict({"defaults": {"max_error_ratio": 0.02, "p95_ms": 300}, "services": {"payment-service": {}}})
    v = Verifier(FakeDetector(after_snaps), slos, timeout_s=0.5, min_settle_s=0)  # type: ignore[arg-type]
    return (await v.verify("INC-T", "act-1", ["payment-service"], before)).outcome


async def test_verification_outcomes() -> None:
    before = {"payment-service": signals("payment-service", ("error_ratio", 0.3), error_ratio=0.3)}
    healthy = {"payment-service": signals("payment-service", error_ratio=0.0)}
    worse = {"payment-service": signals("payment-service", ("error_ratio", 0.9), error_ratio=0.9, ready=0)}
    same = {"payment-service": signals("payment-service", ("error_ratio", 0.29), error_ratio=0.29)}
    assert await _verify([healthy] * 6, before) == VerificationOutcome.RESOLVED
    assert await _verify([worse] * 6, before) == VerificationOutcome.DEGRADED
    assert await _verify([same] * 6, before) == VerificationOutcome.NO_EFFECT

