"""Deterministic diagnosis: each fault class is identified from generic evidence shapes."""

from __future__ import annotations

from collections import Counter

import pytest

from aegis.domain.enums import CauseCategory
from aegis.engine.diagnosis.engine import DiagnosisEngine
from aegis.engine.signatures import LogSignature, TraceAnalysis
from tests.conftest import bundle, change, signals, workloads


async def diagnose(b):  # type: ignore[no-untyped-def]
    return await DiagnosisEngine(router=None).diagnose("INC-T", "test", b, 1)


async def test_bad_deployment_identified_on_the_changed_service() -> None:
    sig = {"payment-service": signals("payment-service", ("error_ratio", 0.34)),
           "order-service": signals("order-service", ("error_ratio", 0.3)),
           "api-gateway": signals("api-gateway", ("error_ratio", 0.2))}
    traces = TraceAnalysis(traces=20, error_traces=20, error_origin_services=Counter({"payment-service": 18}))
    b = bundle(sig, changes=[change("payment-service", "release", [("containers.payment-service.image", "shopflow:1.0.0",
                                                                     "shopflow:1.1.0")])],
               workloads=workloads(payment_service=2), traces=traces)
    dx = await diagnose(b)
    assert dx.selected is not None
    assert dx.selected.category == CauseCategory.BAD_DEPLOYMENT
    assert dx.selected.component == "payment-service"
    assert dx.selected.supporting_evidence, "diagnosis must cite evidence"
    assert set(dx.selected.supporting_evidence) <= {e.id for e in b.evidence}
    assert "order-service" in dx.blast_radius and "api-gateway" in dx.blast_radius


async def test_change_after_onset_is_contradicted() -> None:
    sig = {"payment-service": signals("payment-service", ("error_ratio", 0.34))}
    b = bundle(sig, changes=[change("payment-service", "release", [("containers.x.image", "a", "b")], seconds_before=-300)],
               workloads=workloads(payment_service=2))
    dx = await diagnose(b)
    assert not (dx.selected and dx.selected.category == CauseCategory.BAD_DEPLOYMENT)


async def test_configmap_change_with_crashloop_is_config_error() -> None:
    sig = {"auth-service": signals("auth-service", ("crashloop", 1), ("unavailable_replicas", 1), ready=1, crashloop_pods=1)}
    logs = [LogSignature(service="auth-service", signature="startup failed: ConfigurationError: configuration invalid: "
                         "AUTH_CONFIG is not valid JSON", level="critical", count=6, sample="configuration invalid")]
    b = bundle(sig, changes=[change("auth-config", "config", [("data.AUTH_CONFIG", "{...}", "{")], kind="ConfigMap"),
                             change("auth-service", "restart", [("template.annotations.kubectl.kubernetes.io/restartedAt", "", "t")],
                                    cid="chg-2")],
               workloads=workloads(auth_service=2), logs=logs)
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.CONFIG_ERROR
    assert dx.selected.component == "auth-service"


async def test_scaled_to_zero_dependency_is_root_not_its_callers() -> None:
    sig = {"redis": signals("redis", ("scaled_to_zero", 0), ready=0, desired=0, rps=None),
           "auth-service": signals("auth-service", ("error_ratio", 0.5)),
           "api-gateway": signals("api-gateway", ("error_ratio", 0.3))}
    b = bundle(sig, changes=[change("redis", "scale", [("spec.replicas", "1", "0")])], workloads=workloads())
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.DEPENDENCY_FAILURE
    assert dx.selected.component == "redis"


async def test_network_path_degradation_detected_from_client_server_divergence() -> None:
    sig = {"order-service": signals("order-service", ("latency_p95", 1100), p95_ms=1100),
           "api-gateway": signals("api-gateway", ("latency_p95", 1200), p95_ms=1200),
           "payment-service": signals("payment-service", p95_ms=25)}
    b = bundle(sig, edge_metrics={"order-service->payment-service": {"p95_ms": 950, "error_ratio": 0.0, "baseline_p95_ms": 30}},
               workloads=workloads())
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.NETWORK_DEGRADATION
    assert dx.selected.component == "payment-service"
    assert dx.selected.edge == "order-service->payment-service"


async def test_traffic_surge_is_cpu_saturation() -> None:
    s = signals("payment-service", ("cpu_saturation", 0.97), ("latency_p95", 900), cpu_util=0.97, rps=20.0,
                rps_baseline=2.5, p95_ms=900)
    b = bundle({"payment-service": s}, workloads=workloads())
    from aegis.domain.enums import EvidenceKind
    from aegis.domain.schemas import Evidence, EvidenceSource
    from tests.conftest import NOW
    b.add("traffic:payment-service", Evidence(id="ev-traffic", kind=EvidenceKind.METRIC, service="payment-service",
                                               title="traffic", summary="8x", data={"ratio": 8.0},
                                               source=EvidenceSource(system="prometheus"), observed_at=NOW))
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.CPU_SATURATION
    assert dx.selected.component == "payment-service"


async def test_pool_exhaustion_after_release() -> None:
    s = signals("order-service", ("db_pool_saturation", 1.0), ("error_ratio", 0.2), db_pool_util=1.0, db_waiting=4)
    logs = [LogSignature(service="order-service", signature="PoolExhaustedError: database pool exhausted: timed out acquiring",
                         level="error", count=40, sample="pool exhausted")]
    b = bundle({"order-service": s}, changes=[change("order-service", "env", [("containers.o.env.X", "a", "b")])],
               workloads=workloads(order_service=2), logs=logs)
    dx = await diagnose(b)
    top = {(h.category, h.component) for h in dx.hypotheses[:2]}
    assert (CauseCategory.DB_CONNECTION_EXHAUSTION, "order-service") in top
    pool = next(h for h in dx.hypotheses if h.category == CauseCategory.DB_CONNECTION_EXHAUSTION)
    assert pool.trigger_change, "mechanism should be linked to the triggering deployment"


async def test_weak_evidence_yields_no_autonomous_selection() -> None:
    b = bundle({"api-gateway": signals("api-gateway", ("traffic_drop", 0.1))}, workloads=workloads())
    dx = await diagnose(b)
    assert dx.selected is None
    assert "confidence" in dx.uncertainty.lower() or dx.hypotheses[0].category == CauseCategory.UNKNOWN


@pytest.mark.parametrize("ttl_change", [True])
async def test_resource_limit_reduction(ttl_change: bool) -> None:
    s = signals("inventory-service", ("crashloop", 1), ("oom_killed", 1), crashloop_pods=1, oom_recent=1)
    b = bundle({"inventory-service": s}, changes=[change("inventory-service", "resources", [
        ("containers.inventory-service.resources.limits.memory", "192Mi", "48Mi"),
        ("containers.inventory-service.resources.requests.memory", "96Mi", "32Mi")])], workloads=workloads(inventory_service=2))
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.RESOURCE_MISCONFIGURATION


async def test_redeployed_failing_dependency_is_not_a_network_fault() -> None:
    """Regression (observed live): a bad release also caused client-side latency; the release must win."""
    sig = {"order-service": signals("order-service", ("error_ratio", 0.25), ("latency_p95", 4500), p95_ms=4500),
           "payment-service": signals("payment-service", ("error_ratio", 0.3), error_ratio=0.3, p95_ms=40)}
    b = bundle(sig, changes=[change("payment-service", "release", [("containers.p.image", "a:1", "a:2")])],
               edge_metrics={"order-service->payment-service": {"p95_ms": 3750, "error_ratio": 0.1, "baseline_p95_ms": 40}},
               workloads=workloads(payment_service=2))
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.BAD_DEPLOYMENT
    assert dx.selected.component == "payment-service"


# --- Regressions found in the live benchmark (live-20260929-010108-efd4) -------------------------


async def test_raised_resources_are_not_a_misconfiguration() -> None:
    """A reset raised order's CPU limit 40m -> 500m; the rule used to call that a reduction."""
    s = signals("order-service", ("error_ratio", 0.09), error_ratio=0.09)
    b = bundle({"order-service": s}, changes=[change("order-service", "resources", [
        ("containers.order-service.resources.limits.cpu", "40m", "500m"),
        ("containers.order-service.resources.requests.cpu", "20m", "50m")], seconds_before=200)],
        workloads=workloads(order_service=2))
    dx = await diagnose(b)
    assert all(h.category != CauseCategory.RESOURCE_MISCONFIGURATION for h in dx.hypotheses)


async def test_rollback_that_ran_healthy_for_minutes_is_not_blamed() -> None:
    """The previous scenario's rollback (1.1.0 -> 1.0.0, re-activating an old ReplicaSet) happened
    155s before an unrelated database-path latency incident and was selected as the cause."""
    from datetime import timedelta

    from tests.conftest import NOW
    w = workloads(order_service=3)
    w["order-service"]["revision"] = 3
    w["order-service"]["revisions"] = [
        {"revision": 3, "podTemplateHash": "h1", "createdAt": (NOW - timedelta(hours=2)).isoformat()},
        {"revision": 2, "podTemplateHash": "h2", "createdAt": (NOW - timedelta(minutes=8)).isoformat()}]
    sig = {"order-service": signals("order-service", ("latency_p95", 3500), p95_ms=3500),
           "api-gateway": signals("api-gateway", ("latency_p95", 970), p95_ms=970)}
    b = bundle(sig, changes=[change("order-service", "release", [("containers.order-service.image", "shopflow:1.1.0",
                                                                  "shopflow:1.0.0")], seconds_before=155)],
               edge_metrics={"order-service->postgres": {"p95_ms": 480, "error_ratio": 0.0, "baseline_p95_ms": 5},
                             "payment-service->postgres": {"p95_ms": 19, "error_ratio": 0.0, "baseline_p95_ms": 20}},
               workloads=w)
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.NETWORK_DEGRADATION
    assert dx.selected.component == "postgres"
    bad = next(h for h in dx.hypotheses if h.category == CauseCategory.BAD_DEPLOYMENT)
    assert bad.contradicting_evidence, "the restoration must be recorded as contradicting evidence"


async def test_new_release_is_still_blamed_when_recent() -> None:
    """Guard for the rule above: a *new* ReplicaSet created by the change is not a restoration."""
    from tests.conftest import NOW
    w = workloads(payment_service=2)
    w["payment-service"]["revision"] = 2
    w["payment-service"]["revisions"] = [{"revision": 2, "podTemplateHash": "h2", "createdAt": NOW.isoformat()},
                                         {"revision": 1, "podTemplateHash": "h1", "createdAt": "2026-08-01T00:00:00+00:00"}]
    b = bundle({"payment-service": signals("payment-service", ("error_ratio", 0.34))},
               changes=[change("payment-service", "release", [("containers.p.image", "1.0.0", "1.1.0")], seconds_before=30)],
               workloads=w)
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.BAD_DEPLOYMENT


async def test_throttled_service_under_surge_is_cpu_saturation_not_network() -> None:
    """Payment at 43% average CPU but 40% of CFS periods throttled under 4x traffic: its own
    client-side timings to postgres looked like a network fault."""
    s = signals("payment-service", ("latency_p95", 850), p95_ms=850, cpu_util=0.43, throttle_ratio=0.40,
                rps=12.8, rps_baseline=3.0)
    sig = {"payment-service": s, "order-service": signals("order-service", ("latency_p95", 900), p95_ms=900)}
    b = bundle(sig, edge_metrics={"payment-service->postgres": {"p95_ms": 400, "error_ratio": 0.0, "baseline_p95_ms": 20},
                                  "order-service->postgres": {"p95_ms": 5, "error_ratio": 0.0, "baseline_p95_ms": 5}},
               workloads=workloads())
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.CPU_SATURATION
    assert dx.selected.component == "payment-service"


async def test_connection_resets_with_modest_error_ratio_are_network() -> None:
    """4% of order->payment calls failed with transport errors while payment itself was healthy."""
    sig = {"order-service": signals("order-service", ("error_ratio", 0.09), error_ratio=0.09),
           "api-gateway": signals("api-gateway", ("error_ratio", 0.034), error_ratio=0.034)}
    logs = [LogSignature(service="order-service", level="error", count=8, sample="x",
                         signature="DependencyError: upstream payment-service unreachable: RemoteProtocolError")]
    b = bundle(sig, edge_metrics={"order-service->payment-service": {"p95_ms": 90, "error_ratio": 0.043,
                                                                    "baseline_p95_ms": 90}},
               logs=logs, workloads=workloads())
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.NETWORK_DEGRADATION
    assert dx.selected.component == "payment-service"


async def test_timeouts_to_a_starved_dependency_are_not_network() -> None:
    """Read timeouts are what a CPU-starved dependency produces; they must not make a network case."""
    sig = {"api-gateway": signals("api-gateway", ("error_ratio", 0.04), error_ratio=0.04),
           "order-service": signals("order-service", ("unavailable_replicas", 1), ready=1, throttle_ratio=0.63)}
    logs = [LogSignature(service="api-gateway", level="error", count=8, sample="x",
                         signature="DependencyError: upstream order-service timed out (ReadTimeout)")]
    b = bundle(sig, changes=[change("order-service", "resources", [
        ("containers.order-service.resources.limits.cpu", "500m", "40m")], seconds_before=60)],
        edge_metrics={"api-gateway->order-service": {"p95_ms": 110, "error_ratio": 0.03, "baseline_p95_ms": 100}},
        logs=logs, workloads=workloads(order_service=2))
    dx = await diagnose(b)
    assert dx.selected and dx.selected.category == CauseCategory.RESOURCE_MISCONFIGURATION
    assert dx.selected.component == "order-service"
