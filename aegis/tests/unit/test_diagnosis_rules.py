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
