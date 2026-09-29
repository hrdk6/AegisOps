"""Service dependency graph.

Edges come from two independent sources and are reconciled:
  * declared  — aegisops.io/dependencies annotations (via the control plane)
  * observed  — client-side dependency metrics (Prometheus) and trace-derived
                service calls (Jaeger)
The graph answers blast-radius ("who degrades if X is unhealthy?") and
explanation ("which dependency could explain X's symptom?") queries and
identifies root-cause candidates (deepest anomalous nodes).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aegis.clients.controlplane import ControlPlane
from aegis.clients.http import UpstreamUnavailable
from aegis.clients.jaeger import Jaeger
from aegis.clients.prometheus import Prometheus

log = logging.getLogger("aegis.topology")


@dataclass
class Edge:
    source: str
    target: str
    declared: bool = False
    observed_metrics: bool = False
    observed_traces: bool = False
    rps: float = 0.0

    @property
    def observed(self) -> bool:
        return self.observed_metrics or self.observed_traces

    def as_dict(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "declared": self.declared,
                "observedMetrics": self.observed_metrics, "observedTraces": self.observed_traces,
                "rps": round(self.rps, 3),
                "status": "confirmed" if self.declared and self.observed else
                          "declared-only" if self.declared else "undeclared"}


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str], Edge] = field(default_factory=dict)
    built_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def add_edge(self, source: str, target: str) -> Edge:
        self.nodes.setdefault(source, {"name": source})
        self.nodes.setdefault(target, {"name": target})
        return self.edges.setdefault((source, target), Edge(source, target))

    def dependencies(self, node: str) -> list[str]:
        return sorted(t for (s, t) in self.edges if s == node)

    def dependents(self, node: str) -> list[str]:
        return sorted(s for (s, t) in self.edges if t == node)

    def _walk(self, start: str, step) -> list[str]:  # type: ignore[no-untyped-def]
        seen: set[str] = set()
        queue = deque([start])
        while queue:
            for nxt in step(queue.popleft()):
                if nxt not in seen and nxt != start:
                    seen.add(nxt)
                    queue.append(nxt)
        return sorted(seen)

    def upstream(self, node: str) -> list[str]:
        """Transitive dependents: the blast radius if `node` is unhealthy."""
        return self._walk(node, self.dependents)

    def downstream(self, node: str) -> list[str]:
        """Transitive dependencies: everything that could explain `node`'s symptoms."""
        return self._walk(node, self.dependencies)

    def distance(self, source: str, target: str) -> int | None:
        seen = {source: 0}
        queue = deque([source])
        while queue:
            cur = queue.popleft()
            if cur == target:
                return seen[cur]
            for nxt in self.dependencies(cur):
                if nxt not in seen:
                    seen[nxt] = seen[cur] + 1
                    queue.append(nxt)
        return None

    def root_candidates(self, anomalous: set[str]) -> list[str]:
        """Anomalous nodes with no anomalous (transitive) dependency — the deepest
        point of a failure cascade."""
        return sorted(n for n in anomalous if not (set(self.downstream(n)) & anomalous))

    def explain(self, symptom: str, anomalous: set[str]) -> list[dict[str, Any]]:
        """Rank dependencies of `symptom` that could explain it (anomalous first, then by distance)."""
        out = []
        for dep in self.downstream(symptom):
            out.append({"service": dep, "distance": self.distance(symptom, dep), "anomalous": dep in anomalous,
                        "path": self.path(symptom, dep)})
        out.sort(key=lambda d: (not d["anomalous"], d["distance"] or 99))
        return out

    def path(self, source: str, target: str) -> list[str]:
        prev: dict[str, str | None] = {source: None}
        queue = deque([source])
        while queue:
            cur = queue.popleft()
            if cur == target:
                break
            for nxt in self.dependencies(cur):
                if nxt not in prev:
                    prev[nxt] = cur
                    queue.append(nxt)
        if target not in prev:
            return []
        path, cur = [], target
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        return list(reversed(path))

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes.values()), "edges": [e.as_dict() for e in self.edges.values()],
                "builtAt": self.built_at.isoformat()}


class TopologyBuilder:
    def __init__(self, cp: ControlPlane, prom: Prometheus, jaeger: Jaeger | None, namespace: str) -> None:
        self.cp = cp
        self.prom = prom
        self.jaeger = jaeger
        self.namespace = namespace
        self.cached: Graph | None = None

    async def build(self) -> Graph:
        g = Graph()
        declared = await self.cp.topology(self.namespace)
        for n in declared.get("nodes", []):
            g.nodes[n["name"]] = {"name": n["name"], "tier": n.get("tier"), "replicas": n.get("replicas"),
                                  "ready": n.get("ready")}
        for e in declared.get("edges", []):
            g.add_edge(e["from"], e["to"]).declared = True
        try:
            q = (f'sum by (service, dependency) (rate(shop_dependency_requests_total{{namespace="{self.namespace}"}}[5m]))')
            for labels, rps in await self.prom.vector(q):
                if rps > 0 and labels.get("service") and labels.get("dependency"):
                    edge = g.add_edge(labels["service"], labels["dependency"])
                    edge.observed_metrics = True
                    edge.rps = rps
        except UpstreamUnavailable as exc:
            log.warning("metric-derived edges unavailable", extra={"fields": {"error": exc.detail}})
        if self.jaeger is not None:
            try:
                for dep in await self.jaeger.dependencies(datetime.now(UTC)):
                    parent, child = dep.get("parent"), dep.get("child")
                    if parent and child and parent != child and parent in g.nodes and child in g.nodes:
                        g.add_edge(parent, child).observed_traces = True
            except UpstreamUnavailable as exc:
                log.warning("trace-derived edges unavailable", extra={"fields": {"error": exc.detail}})
        self.cached = g
        return g
