from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aegis.domain.enums import EvidenceKind
from aegis.domain.schemas import Anomaly, Evidence, EvidenceSource, ServiceSignals
from aegis.engine.context import ContextBundle
from aegis.engine.signatures import LogSignature, TraceAnalysis
from aegis.engine.topology import Graph

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

EDGES = [("storefront", "api-gateway"), ("api-gateway", "auth-service"), ("api-gateway", "order-service"),
         ("api-gateway", "inventory-service"), ("order-service", "payment-service"), ("order-service", "inventory-service"),
         ("order-service", "notification-service"), ("order-service", "postgres"), ("order-service", "redis"),
         ("payment-service", "postgres"), ("auth-service", "postgres"), ("auth-service", "redis"),
         ("inventory-service", "postgres"), ("inventory-service", "redis")]
SERVICES = ["storefront", "api-gateway", "auth-service", "order-service", "payment-service", "notification-service",
            "inventory-service", "postgres", "redis"]


def graph() -> Graph:
    g = Graph()
    for s in SERVICES:
        g.nodes[s] = {"name": s}
    for a, b in EDGES:
        e = g.add_edge(a, b)
        e.declared = e.observed_metrics = True
    return g


def signals(name: str, *anoms: tuple[str, float], ready: int = 2, desired: int = 2, since: datetime = NOW,
            **kw: Any) -> ServiceSignals:
    s = ServiceSignals(service=name, at=NOW, ready=ready, desired=desired, rps=kw.pop("rps", 5.0),
                       error_ratio=kw.pop("error_ratio", 0.0), p95_ms=kw.pop("p95_ms", 30.0), **kw)
    s.anomalies = [Anomaly(signal=sig, value=v, reason=f"{sig}={v}", since=since) for sig, v in anoms]
    s.status = "degraded" if anoms else "healthy"
    return s


def bundle(sig: dict[str, ServiceSignals], **kw: Any) -> ContextBundle:
    anomalous = {k for k, v in sig.items() if v.anomalies}
    g = kw.pop("graph", graph())
    for name in SERVICES:
        sig.setdefault(name, signals(name))
    b = ContextBundle(incident_id="INC-T", onset=NOW, start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=2),
                      signals=sig, anomalous=anomalous, graph=g, scope=SERVICES, **kw)
    # Minimal evidence mirroring what the context engine emits.
    for name, s in sig.items():
        for a in s.anomalies:
            b.add(f"metric:{name}:{a.signal}", Evidence(
                id=Evidence.make_id("metric", name, a.signal), kind=EvidenceKind.METRIC, service=name, signal=a.signal,
                title=a.reason, summary=a.reason, source=EvidenceSource(system="prometheus"), observed_at=NOW))
        if not s.anomalies:
            b.add(f"healthy:{name}", Evidence(
                id=Evidence.make_id("metric", name, "healthy"), kind=EvidenceKind.METRIC, service=name, signal="healthy",
                title=f"{name} healthy", summary=f"{name} healthy", source=EvidenceSource(system="prometheus"), observed_at=NOW))
    for c in b.changes:
        b.add(f"change:{c['id']}", Evidence(
            id=Evidence.make_id("change", c["name"], c["type"], c["id"]), kind=EvidenceKind.CHANGE, service=c["name"],
            signal=f"change.{c['type']}", title="change", summary="change",
            data={"kind": c["kind"], "type": c["type"], "fields": c.get("fields", [])},
            source=EvidenceSource(system="controlplane"), observed_at=NOW))
    roots = b.graph.root_candidates(b.anomalous)
    b.add("topology:roots", Evidence(id="ev-topology", kind=EvidenceKind.TOPOLOGY, title="roots", summary=",".join(roots),
                                     source=EvidenceSource(system="aegisops"), observed_at=NOW))
    for e in b.edge_metrics:
        b.add(f"edge:{e}", Evidence(id=Evidence.make_id("metric", e.split("->")[0], "dependency", e), kind=EvidenceKind.METRIC,
                                    title=e, summary=e, source=EvidenceSource(system="prometheus"), observed_at=NOW))
    return b


def change(name: str, typ: str, fields: list[tuple[str, str, str]], seconds_before: float = 60, kind: str = "Deployment",
           cid: str = "chg-1") -> dict[str, Any]:
    return {"id": cid, "time": (NOW - timedelta(seconds=seconds_before)).isoformat(), "namespace": "shop", "kind": kind,
            "name": name, "type": typ, "fields": [{"path": p, "old": o, "new": n} for p, o, n in fields]}


def workloads(**revisions: int) -> dict[str, dict[str, Any]]:
    out = {}
    for s in SERVICES:
        n = revisions.get(s.replace("-", "_"), 1)
        out[s] = {"name": s, "replicas": 2, "readyReplicas": 2, "rolloutComplete": True, "simulationEnabled": True, "revision": n,
                  "revisions": [{"revision": i, "podTemplateHash": f"h{i}"} for i in range(n, 0, -1)],
                  "containers": [{"name": s, "configMaps": ["auth-config"] if s == "auth-service" else [], "env": {}}]}
    return out


@pytest.fixture
def logsig() -> type[LogSignature]:
    return LogSignature


@pytest.fixture
def trace_analysis() -> type[TraceAnalysis]:
    return TraceAnalysis
