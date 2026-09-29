"""Detection semantics, telemetry-poisoning resistance, redaction, auth and approval signing."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from aegis.chaos.scenarios import load_scenarios
from aegis.engine.detector import DetectorState, Observation, compute_signals
from aegis.engine.signatures import analyze_traces, cluster_logs
from aegis.engine.slo import SLOBook
from aegis.security.auth import (
    Permission,
    decode_token,
    hash_password,
    issue_token,
    parse_bootstrap_users,
    permissions_for,
    verify_password,
)
from aegis.security.redaction import redact, redact_obj, sanitize_untrusted
from aegis.security.signing import sign_decision, sign_mode_change
from tests.conftest import REPO

SLOS = SLOBook.from_dict({"defaults": {"max_error_ratio": 0.02, "p95_ms": 500, "min_rps": 0.2},
                          "services": {"payment-service": {"p95_ms": 300}}})
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def obs(i: int, err: float, desired: int = 2, ready: int = 2, pods: list | None = None) -> Observation:
    return Observation(at=T0 + timedelta(seconds=5 * i),
                       metrics={"rps": {"payment-service": 10.0}, "errors": {"payment-service": 10.0 * err},
                                "p95": {"payment-service": 0.05}},
                       workloads=[{"name": "payment-service", "replicas": desired, "readyReplicas": ready,
                                   "containers": [{"name": "payment-service"}], "labels": {}, "pods": pods or []}])


def test_error_breach_requires_persistence() -> None:
    st = DetectorState()
    first = compute_signals(obs(0, 0.3), SLOS, st, persistence=2)["payment-service"]
    second = compute_signals(obs(1, 0.3), SLOS, st, persistence=2)["payment-service"]
    assert not first.anomalies, "single noisy sample must not page"
    assert [a.signal for a in second.anomalies] == ["error_ratio"]
    assert second.anomalies[0].since == T0, "onset is the first observation of the streak"


def test_crashloop_is_immediate_and_scale_to_zero_detected() -> None:
    st = DetectorState()
    for i in range(8):
        compute_signals(obs(i, 0.0), SLOS, st)
    pods = [{"name": "p", "waitingReason": "CrashLoopBackOff", "restarts": 4}]
    s = compute_signals(obs(9, 0.0, pods=pods), SLOS, st)["payment-service"]
    assert "crashloop" in [a.signal for a in s.anomalies]
    z = compute_signals(obs(10, 0.0, desired=0, ready=0), SLOS, st)["payment-service"]
    assert "scaled_to_zero" in [a.signal for a in z.anomalies] and z.status == "down"


def test_baseline_is_not_poisoned_during_incident() -> None:
    st = DetectorState()
    for i in range(10):
        compute_signals(obs(i, 0.0), SLOS, st)
    base_before = st.baseline("payment-service", "error_ratio")
    for i in range(10, 40):
        compute_signals(obs(i, 0.5), SLOS, st)
    assert st.baseline("payment-service", "error_ratio") == base_before == 0.0


def test_missing_metrics_are_unknown_not_zero_traffic() -> None:
    """Regression (live): when Prometheus queries failed, every service was flagged with
    "request rate 0.00/s vs baseline" and could have opened a bogus incident."""
    st = DetectorState()
    for i in range(10):
        compute_signals(obs(i, 0.0), SLOS, st)
    blind = Observation(at=T0 + timedelta(seconds=60), metrics={}, workloads=obs(0, 0.0).workloads)
    for _ in range(4):
        s = compute_signals(blind, SLOS, st)["payment-service"]
    assert s.rps is None and not s.anomalies
    real_drop = obs(20, 0.0)
    real_drop.metrics["rps"] = {"payment-service": 0.0}
    for i in range(4):
        s = compute_signals(real_drop, SLOS, st)["payment-service"]
    assert "traffic_drop" in [a.signal for a in s.anomalies], "a measured drop is still detected"


async def test_one_failing_query_does_not_blind_the_detector() -> None:
    from aegis.clients.http import UpstreamUnavailable
    from aegis.engine.detector import QUERIES, Detector

    class Prom:
        async def by_label(self, query: str, label: str) -> dict[str, float]:
            if "container_memory_working_set_bytes" in query:
                raise UpstreamUnavailable("prometheus", "422 many-to-one matching must be explicit")
            return {"payment-service": 1.0}

    det = Detector.__new__(Detector)
    det.prom, det.namespace, det.degraded_sources = Prom(), "shop", set()
    out = await det._metrics()
    assert "mem_util" not in out and set(out) == set(QUERIES) - {"mem_util"}
    assert det.degraded_sources == {"prometheus:mem_util"}


def test_redaction() -> None:
    text = "dsn=postgresql://shop:s3cr3t@db:5432/x token=abcd1234efgh Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdefghij"
    out = redact(text)
    assert "s3cr3t" not in out and "abcd1234efgh" not in out and "eyJhbGci" not in out
    assert redact_obj({"db_password": "p", "nested": [{"api_key": "k"}], "ok": "fine"}) == {
        "db_password": "[REDACTED]", "nested": [{"api_key": "[REDACTED]"}], "ok": "fine"}
    assert "\x1b" not in sanitize_untrusted("hello\x1b[31mred")
    assert len(sanitize_untrusted("x" * 5000, 100)) <= 101


def test_password_hashing_and_tokens() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) and not verify_password("wrong", h)
    tok, exp = issue_token("k" * 40, "alice", "approver", 5)
    claims = decode_token("k" * 40, tok)
    assert claims["sub"] == "alice" and exp > time.time()
    with pytest.raises(jwt.PyJWTError):
        decode_token("other-key" * 5, tok)
    expired = jwt.encode({"sub": "a", "role": "viewer", "iat": 1, "exp": 2, "iss": "aegisops"}, "k" * 40, algorithm="HS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token("k" * 40, expired)


def test_rbac_matrix() -> None:
    assert Permission.APPROVE not in permissions_for("viewer")
    assert Permission.APPROVE not in permissions_for("operator")
    assert Permission.APPROVE in permissions_for("approver")
    assert Permission.POLICY_ADMIN not in permissions_for("approver")
    assert Permission.POLICY_ADMIN in permissions_for("admin")
    assert permissions_for("root") == set()


def test_weak_bootstrap_passwords_rejected() -> None:
    with pytest.raises(ValueError):
        parse_bootstrap_users("admin:short:admin")
    assert parse_bootstrap_users("a:long-enough-password:viewer") == [("a", "long-enough-password", "viewer")]


def test_approval_signature_matches_controlplane_format() -> None:
    """Cross-language contract: identical payload layout to controlplane/internal/approval."""
    key = b"test-approval-key-0123456789abcdef"
    signed = sign_decision(key, "act-1", "spec-hash-1", "approved", "alice", "ok", issued_at=1_800_000_000)
    reason_hash = hashlib.sha256(b"ok").hexdigest()
    payload = "\n".join(["aegisops-approval-v1", "act-1", "spec-hash-1", "approved", "alice", "1800000000", reason_hash])
    assert signed["signature"] == hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def test_signing_matches_go_verifier_vector() -> None:
    """Same vector as controlplane/internal/approval TestCrossLanguageVector (Go verifies these MACs)."""
    key = b"aegisops-test-vector-key-0123456789abcdef"
    d = sign_decision(key, "act-inc-1-abc123", "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "approved", "alice",
                      "rollback verified in sandbox", issued_at=1_800_000_000)
    assert d["signature"] == "e7d1f17efb87b5c59a35d4c9698090144f929e3d6dd9bb264af166e43113aa25"
    m = sign_mode_change(key, "supervised", "admin", issued_at=1_800_000_000)
    assert m["signature"] == "b8b83dbbb99ecddc9e971f63339e36779b7ae560b7f62686ce1cdd4d85b723a9"


def test_log_signatures_cluster_numbers_and_ids() -> None:
    lines = [{"ts": i, "fields": {"service": "order-service", "level": "error",
                                  "msg": f"database pool exhausted: timed out after 2.0s (in_use={i}/8)", "trace_id": f"t{i}"}}
             for i in range(5)]
    sigs = cluster_logs(lines)
    assert len(sigs) == 1 and sigs[0].count == 5 and len(sigs[0].trace_ids) == 3


def test_trace_analysis_finds_error_origin_and_network_gap() -> None:
    trace = {"traceID": "t1", "processes": {"p1": {"serviceName": "order-service"}, "p2": {"serviceName": "payment-service"}},
             "spans": [
                 {"spanID": "a", "operationName": "POST /orders", "processID": "p1", "duration": 1_000_000, "startTime": 1,
                  "tags": [{"key": "error", "value": True}], "references": []},
                 {"spanID": "b", "operationName": "POST", "processID": "p1", "duration": 950_000, "startTime": 2,
                  "tags": [{"key": "span.kind", "value": "client"}, {"key": "error", "value": True}],
                  "references": [{"refType": "CHILD_OF", "spanID": "a"}]},
                 {"spanID": "c", "operationName": "POST /charge", "processID": "p2", "duration": 30_000, "startTime": 3,
                  "tags": [{"key": "span.kind", "value": "server"}, {"key": "otel.status_code", "value": "ERROR"}],
                  "references": [{"refType": "CHILD_OF", "spanID": "b"}]}]}
    a = analyze_traces([trace])
    assert a.error_traces == 1 and a.origin_fraction("payment-service") == 1.0
    assert a.mean_gap("order-service->payment-service") == pytest.approx(920.0)


def test_scenario_catalog_is_valid_and_complete() -> None:
    scenarios = load_scenarios(Path(REPO) / "benchmarks" / "scenarios")
    assert len(scenarios) >= 20
    classes = {s.fault_class for s in scenarios.values()}
    assert {"bad_deployment", "network", "dependency_outage", "memory", "saturation", "canary", "config_error"} <= classes
    for s in scenarios.values():
        assert s.expected.outcomes
        if "resolved" in s.expected.outcomes and s.expected.outcomes == ["resolved"]:
            assert s.expected.root_cause and s.expected.remediation.acceptable, s.id
        assert any(r.target == "postgres" for r in s.expected.remediation.prohibited), s.id


def test_framework_send_spans_are_not_blamed() -> None:
    """Regression (observed live): ASGI 'http send' spans carry the 5xx status on every hop and start
    last; the origin must still be the deepest real failing span (payment-service)."""
    def span(sid: str, parent: str | None, svc: str, op: str, kind: str, err: bool, start: int) -> dict:
        tags = [{"key": "span.kind", "value": kind}] + ([{"key": "error", "value": True}] if err else [])
        refs = [{"refType": "CHILD_OF", "spanID": parent}] if parent else []
        return {"spanID": sid, "processID": svc, "operationName": op, "tags": tags, "references": refs,
                "startTime": start, "duration": 100}
    spans = [span("s1", None, "sf", "POST /checkout", "server", True, 1),
             span("s2", "s1", "sf", "POST", "client", True, 2),
             span("g1", "s2", "gw", "POST /api/orders", "server", True, 3),
             span("g2", "g1", "gw", "POST", "client", True, 4),
             span("o1", "g2", "or", "POST /orders", "server", True, 5),
             span("o2", "o1", "or", "POST", "client", True, 6),
             span("p1", "o2", "pa", "POST /charge", "server", True, 7),
             span("p2", "p1", "pa", "POST /charge http send", "internal", True, 8),
             span("s9", "s1", "sf", "POST /checkout http send", "internal", True, 99)]
    procs = {"sf": {"serviceName": "storefront"}, "gw": {"serviceName": "api-gateway"},
             "or": {"serviceName": "order-service"}, "pa": {"serviceName": "payment-service"}}
    a = analyze_traces([{"traceID": "t", "spans": spans, "processes": procs}])
    assert a.origin_fraction("payment-service") == 1.0
