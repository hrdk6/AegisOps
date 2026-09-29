"""Context engine: collects, correlates and ranks evidence for an incident.

Every evidence item carries provenance (system, query, time range) so that each
diagnosis claim can be traced back to reproducible telemetry. Collection is
bounded by time window, service scope (anomalous services and their
dependencies) and per-source caps; unavailable sources are recorded as gaps
rather than silently ignored.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aegis.clients.controlplane import ControlPlane
from aegis.clients.http import UpstreamUnavailable
from aegis.clients.jaeger import Jaeger
from aegis.clients.loki import Loki
from aegis.clients.prometheus import Prometheus
from aegis.domain.enums import EvidenceKind
from aegis.domain.schemas import Evidence, EvidenceSource, ServiceSignals
from aegis.engine.signatures import LogSignature, TraceAnalysis, analyze_traces, cluster_logs
from aegis.engine.slo import SLOBook
from aegis.engine.topology import Graph, TopologyBuilder

log = logging.getLogger("aegis.context")

DATASTORES = ("postgres", "redis")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


@dataclass
class ContextBundle:
    incident_id: str
    onset: datetime
    start: datetime
    end: datetime
    signals: dict[str, ServiceSignals]
    anomalous: set[str]
    graph: Graph
    scope: list[str]
    workloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    k8s_signals: list[dict[str, Any]] = field(default_factory=list)
    rs_errors: dict[str, dict[str, float]] = field(default_factory=dict)
    edge_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    logs: list[LogSignature] = field(default_factory=list)
    traces: TraceAnalysis = field(default_factory=TraceAnalysis)
    canaries: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def add(self, key: str, ev: Evidence) -> str:
        if key in self.keys:
            return self.keys[key]
        self.keys[key] = ev.id
        self.evidence.append(ev)
        return ev.id

    # ------------------------------------------------ replay serialization --
    def to_dict(self) -> dict[str, Any]:
        """Complete, JSON-serializable snapshot used for offline replay benchmarks."""
        return {
            "incident_id": self.incident_id, "onset": self.onset.isoformat(), "start": self.start.isoformat(),
            "end": self.end.isoformat(), "signals": {k: v.model_dump(mode="json") for k, v in self.signals.items()},
            "anomalous": sorted(self.anomalous), "scope": self.scope,
            "graph": {"nodes": list(self.graph.nodes.values()),
                      "edges": [{"from": e.source, "to": e.target, "declared": e.declared, "observedMetrics": e.observed_metrics,
                                 "observedTraces": e.observed_traces, "rps": e.rps} for e in self.graph.edges.values()]},
            "workloads": self.workloads, "changes": self.changes, "k8s_signals": self.k8s_signals[-200:],
            "rs_errors": self.rs_errors, "edge_metrics": self.edge_metrics, "logs": [s.as_dict() for s in self.logs],
            "traces": {"traces": self.traces.traces, "error_traces": self.traces.error_traces,
                       "error_origins": dict(self.traces.error_origins),
                       "error_origin_services": dict(self.traces.error_origin_services),
                       "self_time_ms": dict(self.traces.self_time_ms), "edge_gap_ms": dict(self.traces.edge_gap_ms),
                       "sample_trace_ids": self.traces.sample_trace_ids},
            "canaries": self.canaries, "history": self.history,
            "evidence": [e.model_dump(mode="json") for e in self.evidence], "keys": self.keys, "gaps": self.gaps,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextBundle:
        from collections import Counter

        from aegis.engine.topology import Edge

        g = Graph()
        for n in d["graph"]["nodes"]:
            g.nodes[n["name"]] = n
        for e in d["graph"]["edges"]:
            g.edges[(e["from"], e["to"])] = Edge(e["from"], e["to"], e["declared"], e["observedMetrics"],
                                                 e["observedTraces"], e.get("rps", 0.0))
        t = d["traces"]
        traces = TraceAnalysis(traces=t["traces"], error_traces=t["error_traces"], error_origins=Counter(t["error_origins"]),
                               error_origin_services=Counter(t["error_origin_services"]),
                               self_time_ms=Counter(t["self_time_ms"]), sample_trace_ids=t["sample_trace_ids"])
        for k, v in t["edge_gap_ms"].items():
            traces.edge_gap_ms[k] = list(v)
        logs = [LogSignature(service=s["service"], signature=s["signature"], level=s["level"], count=s["count"],
                             first_ns=s["firstSeenNs"], last_ns=s["lastSeenNs"], sample=s["sample"],
                             error_type=s.get("errorType"), trace_ids=s.get("traceIds", [])) for s in d["logs"]]
        return cls(
            incident_id=d["incident_id"], onset=datetime.fromisoformat(d["onset"]), start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            signals={k: ServiceSignals.model_validate(v) for k, v in d["signals"].items()},
            anomalous=set(d["anomalous"]), graph=g, scope=d["scope"], workloads=d["workloads"], changes=d["changes"],
            k8s_signals=d["k8s_signals"], rs_errors=d["rs_errors"], edge_metrics=d["edge_metrics"], logs=logs,
            traces=traces, canaries=d["canaries"], history=d["history"],
            evidence=[Evidence.model_validate(e) for e in d["evidence"]], keys=d["keys"], gaps=d["gaps"])

    def ev(self, key: str) -> str | None:
        return self.keys.get(key)

    def evs(self, *keys: str) -> list[str]:
        return [self.keys[k] for k in keys if k in self.keys]

    def by_id(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.evidence if e.id == evidence_id), None)

    def onset_of(self, service: str) -> datetime:
        s = self.signals.get(service)
        if s and s.anomalies:
            return min(a.since for a in s.anomalies)
        return self.onset

    def configmaps_of(self, workload: str) -> set[str]:
        w = self.workloads.get(workload, {})
        out: set[str] = set()
        for c in w.get("containers", []):
            out.update(c.get("configMaps") or [])
            for v in (c.get("env") or {}).values():
                if isinstance(v, str) and v.startswith("configMapKeyRef:"):
                    out.add(v.split(":", 1)[1].split("/")[0])
        return out

    def changes_for(self, workload: str, lookback: timedelta = timedelta(minutes=15),
                    lookahead: timedelta = timedelta(seconds=90)) -> list[dict[str, Any]]:
        onset = self.onset_of(workload)
        cms = self.configmaps_of(workload)
        out = []
        for c in self.changes:
            t = parse_ts(c.get("time"))
            if t is None or not (onset - lookback <= t <= onset + lookahead):
                continue
            if (c.get("kind") == "Deployment" and c.get("name") == workload) or (
                    c.get("kind") == "ConfigMap" and c.get("name") in cms):
                out.append(c)
        return sorted(out, key=lambda c: c["time"], reverse=True)

    def log_signatures(self, service: str) -> list[LogSignature]:
        return [s for s in self.logs if s.service == service]

    def ranked(self, limit: int = 40) -> list[Evidence]:
        return sorted(self.evidence, key=lambda e: e.score, reverse=True)[:limit]


class ContextEngine:
    def __init__(self, cp: ControlPlane, prom: Prometheus, loki: Loki, jaeger: Jaeger, topology: TopologyBuilder,
                 slos: SLOBook, namespace: str, grafana_url: str = "", jaeger_url: str = "") -> None:
        self.cp, self.prom, self.loki, self.jaeger = cp, prom, loki, jaeger
        self.topology = topology
        self.slos = slos
        self.namespace = namespace
        self.jaeger_url = jaeger_url

    async def collect(self, incident_id: str, signals: dict[str, ServiceSignals], affected: list[str],
                      onset_hint: datetime | None, history: list[dict[str, Any]] | None = None) -> ContextBundle:
        now = datetime.now(UTC)
        # Investigate with the fullest picture: symptoms that are present but not yet confirmed by
        # the detector's persistence rule are included (paging stays conservative; diagnosis does not).
        signals = {k: v.model_copy(update={"anomalies": v.anomalies + v.pending, "pending": []}) for k, v in signals.items()}
        anomalous = {s for s, sig in signals.items() if sig.anomalies} | set(affected)
        onsets = [a.since for s in anomalous if s in signals for a in signals[s].anomalies]
        onset = min(onsets) if onsets else (onset_hint or now)
        if onset_hint and onset_hint < onset:
            onset = onset_hint
        try:
            graph = await self.topology.build()
        except UpstreamUnavailable:
            graph = self.topology.cached or Graph()
        scope = sorted(anomalous | {d for s in anomalous for d in graph.downstream(s)})
        b = ContextBundle(incident_id=incident_id, onset=onset, start=onset - timedelta(minutes=10), end=now,
                          signals=signals, anomalous={s for s in anomalous if s in signals}, graph=graph, scope=scope,
                          history=history or [])
        results = await asyncio.gather(
            self._workloads(b), self._changes(b), self._k8s_signals(b), self._edges(b), self._logs(b),
            self._traces(b), self._canaries(b), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("context source failed", extra={"fields": {"error": repr(r)}})
                b.gaps.append(f"context source error: {type(r).__name__}")
        self._metric_evidence(b)
        await self._replicaset_attribution(b)
        await self._series(b)
        self._topology_evidence(b)
        self._history_evidence(b)
        return b

    # ------------------------------------------------------------ sources --
    async def _workloads(self, b: ContextBundle) -> None:
        try:
            for w in await self.cp.workloads(self.namespace):
                b.workloads[w["name"]] = w
        except UpstreamUnavailable:
            b.gaps.append("control plane unavailable: kubernetes state not collected")
            return
        for name in b.scope:
            w = b.workloads.get(name)
            if not w:
                continue
            crash = [p for p in w.get("pods", []) if p.get("waitingReason") == "CrashLoopBackOff"]
            ooms = [p for p in w.get("pods", []) if p.get("lastTerminationReason") == "OOMKilled"]
            restarts = sum(p.get("restarts", 0) for p in w.get("pods", []))
            if crash or ooms:
                reasons = sorted({p.get("lastTerminationReason") or p.get("waitingReason") for p in crash + ooms} - {None})
                b.add(f"k8s:{name}:pods", Evidence(
                    id=Evidence.make_id("k8s_state", name, "pods"), kind=EvidenceKind.K8S_STATE, service=name,
                    signal="pod_failures", title=f"{name}: {len(crash)} crash-looping, {len(ooms)} OOMKilled pod(s)",
                    summary=(f"{name} pods: {w.get('readyReplicas', 0)}/{w.get('replicas', 0)} ready, {restarts} restarts; "
                             f"termination reasons: {', '.join(reasons) or 'unknown'}"),
                    data={"pods": [{k: p.get(k) for k in ("name", "ready", "restarts", "waitingReason",
                                                          "lastTerminationReason", "lastExitCode", "podTemplateHash")}
                                   for p in w.get("pods", [])], "reasons": reasons},
                    source=EvidenceSource(system="controlplane", query=f"GET /v1/workloads/{self.namespace}/{name}"),
                    observed_at=b.end, score=0.9))
            if w.get("replicas", 0) == 0 or not w.get("rolloutComplete", True):
                b.add(f"k8s:{name}:rollout", Evidence(
                    id=Evidence.make_id("k8s_state", name, "rollout", str(w.get("generation"))), kind=EvidenceKind.K8S_STATE,
                    service=name, signal="rollout_state",
                    title=f"{name}: {w.get('rolloutMessage', '')} (replicas {w.get('replicas')})",
                    summary=(f"{name} desired={w.get('replicas')} ready={w.get('readyReplicas')} "
                             f"updated={w.get('updatedReplicas')} revision={w.get('revision')}: {w.get('rolloutMessage')}"),
                    data={k: w.get(k) for k in ("replicas", "readyReplicas", "updatedReplicas", "revision", "rolloutMessage")},
                    source=EvidenceSource(system="controlplane", query=f"GET /v1/workloads/{self.namespace}/{name}"),
                    observed_at=b.end, score=0.8 if w.get("replicas", 0) == 0 else 0.6))

    async def _changes(self, b: ContextBundle) -> None:
        try:
            b.changes = await self.cp.changes(b.onset - timedelta(minutes=15), self.namespace)
        except UpstreamUnavailable:
            b.gaps.append("control plane unavailable: change history not collected")
            return
        for c in b.changes:
            t = parse_ts(c.get("time"))
            if t is None:
                continue
            delta = (b.onset - t).total_seconds()
            if delta < -120:
                continue  # after onset: consequence (e.g. remediation), not cause
            proximity = 1.0 if 0 <= delta <= 180 else 0.7 if 0 <= delta <= 600 else 0.4
            fields = [f"{f['path']}: {f.get('old') or '<none>'} -> {f.get('new') or '<none>'}" for f in c.get("fields", [])][:8]
            when = f"{abs(delta):.0f}s {'before' if delta >= 0 else 'after'} anomaly onset"
            b.add(f"change:{c['id']}", Evidence(
                id=Evidence.make_id("change", c.get("name"), c.get("type"), c["id"]), kind=EvidenceKind.CHANGE,
                service=c.get("name"), signal=f"change.{c.get('type')}",
                title=f"{c.get('kind')} {c.get('name')} {c.get('type')} change {when}",
                summary=f"{c.get('kind')} {c.get('name')} changed ({c.get('type')}) at {c.get('time')}, {when}: " + "; ".join(fields),
                data={"changeId": c["id"], "kind": c.get("kind"), "type": c.get("type"), "fields": c.get("fields", []),
                      "secondsBeforeOnset": round(delta, 1), "actor": c.get("actor")},
                source=EvidenceSource(system="controlplane", query="GET /v1/changes", start=b.onset - timedelta(minutes=15),
                                      end=b.end),
                observed_at=t, score=0.55 + 0.4 * proximity))

    async def _k8s_signals(self, b: ContextBundle) -> None:
        try:
            items = await self.cp.signals(b.onset - timedelta(minutes=10), self.namespace)
        except UpstreamUnavailable:
            b.gaps.append("control plane unavailable: kubernetes events not collected")
            return
        b.k8s_signals = items
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for s in items:
            if s.get("type") != "Warning":
                continue
            workload = next((w for w in b.scope if s.get("name", "").startswith(w)), None)
            if workload is None:
                continue
            grouped.setdefault((workload, s.get("reason", "")), []).append(s)
        for (workload, reason), group in grouped.items():
            latest = max(group, key=lambda s: s.get("time", ""))
            b.add(f"event:{workload}:{reason}", Evidence(
                id=Evidence.make_id("k8s_event", workload, reason), kind=EvidenceKind.K8S_EVENT, service=workload,
                signal=f"event.{reason}", title=f"{workload}: {reason} x{len(group)}",
                summary=f"Kubernetes reported {reason} for {workload} {len(group)} time(s); latest: {latest.get('message', '')[:240]}",
                data={"reason": reason, "count": len(group), "latest": latest.get("message", "")[:400]},
                source=EvidenceSource(system="kubernetes", query=f"events namespace={self.namespace}", start=b.start, end=b.end),
                observed_at=parse_ts(latest.get("time")) or b.end,
                score=0.75 if reason in ("OOMKilled", "BackOff", "CrashLoopBackOff", "Unhealthy") else 0.5))

    async def _edges(self, b: ContextBundle) -> None:
        ns = f'namespace="{self.namespace}"'
        err_q = (f'sum by (service, dependency) (rate(shop_dependency_requests_total{{{ns},outcome!="ok"}}[1m])) '
                 f'/ sum by (service, dependency) (rate(shop_dependency_requests_total{{{ns}}}[1m]))')
        p95_q = (f'histogram_quantile(0.95, sum by (service, dependency, le) '
                 f'(rate(shop_dependency_request_duration_seconds_bucket{{{ns}}}[1m])))')
        base_q = (f'histogram_quantile(0.95, sum by (service, dependency, le) '
                  f'(rate(shop_dependency_request_duration_seconds_bucket{{{ns}}}[3m] offset {max(60, int((b.end - b.onset).total_seconds()) + 60)}s)))')
        try:
            errs, p95s, base = await asyncio.gather(self.prom.vector(err_q), self.prom.vector(p95_q), self.prom.vector(base_q))
        except UpstreamUnavailable:
            b.gaps.append("prometheus unavailable: dependency metrics not collected")
            return
        for labels, v in errs:
            b.edge_metrics.setdefault(f"{labels['service']}->{labels['dependency']}", {})["error_ratio"] = v
        for labels, v in p95s:
            b.edge_metrics.setdefault(f"{labels['service']}->{labels['dependency']}", {})["p95_ms"] = v * 1000
        for labels, v in base:
            b.edge_metrics.setdefault(f"{labels['service']}->{labels['dependency']}", {})["baseline_p95_ms"] = v * 1000
        for edge, m in b.edge_metrics.items():
            caller, dep = edge.split("->", 1)
            if caller not in b.scope and dep not in b.scope:
                continue
            server = b.signals.get(dep)
            server_p95 = server.p95_ms if server else None
            m["server_p95_ms"] = server_p95 or 0.0
            err, p95, base_p95 = m.get("error_ratio", 0.0), m.get("p95_ms"), m.get("baseline_p95_ms")
            elevated_latency = p95 is not None and p95 > 200 and (base_p95 is None or p95 > 2.5 * max(base_p95, 20))
            if err < 0.02 and not elevated_latency:
                continue
            parts = [f"client-side error ratio {pct(err)}", f"client-side p95 {ms(p95)} (baseline {ms(base_p95)})"]
            if server_p95 is not None:
                parts.append(f"{dep} server-side p95 {ms(server_p95)}")
            score = 0.6 + min(0.3, err) + (0.15 if elevated_latency else 0)
            b.add(f"edge:{edge}", Evidence(
                id=Evidence.make_id("metric", caller, "dependency", edge), kind=EvidenceKind.METRIC, service=caller,
                signal="dependency_health", title=f"{caller} -> {dep}: " + ", ".join(parts[:2]),
                summary=f"Calls from {caller} to {dep}: " + "; ".join(parts),
                data={"caller": caller, "dependency": dep, **{k: round(v, 4) for k, v in m.items()}},
                source=EvidenceSource(system="prometheus", query=p95_q, start=b.start, end=b.end),
                observed_at=b.end, score=min(score, 0.95)))

    async def _logs(self, b: ContextBundle) -> None:
        q = f'{{k8s_namespace_name="{self.namespace}"}} | json | level=~"error|critical|warning"'
        try:
            lines = await self.loki.query_range(q, b.onset - timedelta(minutes=2), b.end, limit=1000)
        except UpstreamUnavailable:
            b.gaps.append("loki unavailable: logs not collected")
            return
        b.logs = [s for s in cluster_logs(lines, top=16) if s.service in b.scope]
        for sig in b.logs[:10]:
            first = datetime.fromtimestamp(sig.first_ns / 1e9, UTC) if sig.first_ns else b.end
            level_weight = {"critical": 0.95, "error": 0.8, "warning": 0.55}.get(sig.level, 0.4)
            b.add(f"log:{sig.service}:{sig.signature}", Evidence(
                id=Evidence.make_id("log", sig.service, sig.signature), kind=EvidenceKind.LOG, service=sig.service,
                signal=f"log.{sig.level}", title=f"{sig.service}: {sig.count}x {sig.level} \"{sig.sample[:90]}\"",
                summary=f"{sig.count} {sig.level} log line(s) from {sig.service} matching \"{sig.signature[:160]}\"; sample: {sig.sample}",
                data=sig.as_dict(),
                source=EvidenceSource(system="loki", query=q, start=b.onset - timedelta(minutes=2), end=b.end),
                observed_at=first, score=min(0.95, level_weight + min(0.1, sig.count / 500))))

    async def _traces(self, b: ContextBundle) -> None:
        start = b.onset - timedelta(minutes=1)
        entries = [s for s in self.slos.entry_services if s in b.signals] or ["api-gateway"]
        targets = list(dict.fromkeys(entries + sorted(b.anomalous & set(self.slos.services))))[:4]
        analysis = TraceAnalysis()
        try:
            for svc in targets:
                analyze_traces(await self.jaeger.traces(svc, start, b.end, errors_only=True, limit=25), analysis)
            slow_floor = int(self.slos.get(entries[0]).p95_ms)
            analyze_traces(await self.jaeger.traces(entries[0], start, b.end, min_duration_ms=slow_floor, limit=20), analysis)
        except UpstreamUnavailable:
            b.gaps.append("jaeger unavailable: traces not analysed")
            return
        b.traces = analysis
        if analysis.traces == 0:
            b.gaps.append("no traces found in the incident window")
            return
        url = f"{self.jaeger_url}/search?service={entries[0]}&tags=%7B%22error%22%3A%22true%22%7D" if self.jaeger_url else None
        if analysis.error_traces:
            origin, count = analysis.error_origin_services.most_common(1)[0]
            b.add("trace:error_origin", Evidence(
                id=Evidence.make_id("trace", origin, "error_origin", str(analysis.error_traces)), kind=EvidenceKind.TRACE,
                service=origin.split("->")[0], signal="trace.error_origin",
                title=f"{count}/{analysis.error_traces} failing traces originate in {origin}",
                summary=(f"Of {analysis.error_traces} error traces, the deepest failing span is in {origin} for "
                         f"{count / analysis.error_traces:.0%}; top origins: "
                         + ", ".join(f"{k} ({v})" for k, v in analysis.error_origins.most_common(3))),
                data=analysis.as_dict(),
                source=EvidenceSource(system="jaeger", query="error=true", url=url, start=start, end=b.end),
                observed_at=b.end, score=0.85))
        if analysis.self_time_ms:
            top, _ = analysis.self_time_ms.most_common(1)[0]
            share = analysis.self_time_fraction(top)
            b.add("trace:self_time", Evidence(
                id=Evidence.make_id("trace", top, "self_time", str(analysis.traces)), kind=EvidenceKind.TRACE,
                service=top.split("->")[0], signal="trace.latency_attribution",
                title=f"{share:.0%} of sampled request time is spent in {top}",
                summary=("Self-time attribution across sampled slow/error traces: "
                         + ", ".join(f"{k} {analysis.self_time_fraction(k):.0%}" for k, _ in analysis.self_time_ms.most_common(4))),
                data=analysis.as_dict(),
                source=EvidenceSource(system="jaeger", query="slow and error traces", url=url, start=start, end=b.end),
                observed_at=b.end, score=0.7))
        for edge, gaps in analysis.edge_gap_ms.items():
            mean_gap = sum(gaps) / len(gaps)
            if mean_gap > 150 and len(gaps) >= 3:
                b.add(f"trace:gap:{edge}", Evidence(
                    id=Evidence.make_id("trace", edge, "gap"), kind=EvidenceKind.TRACE, service=edge.split("->")[0],
                    signal="trace.network_gap",
                    title=f"{edge}: {mean_gap:.0f}ms between client and server spans",
                    summary=(f"Across {len(gaps)} calls {edge}, the client span exceeds the server span by {mean_gap:.0f}ms "
                             f"on average: time spent outside both services (network path or connection handling)."),
                    data={"edge": edge, "meanGapMs": round(mean_gap, 1), "samples": len(gaps)},
                    source=EvidenceSource(system="jaeger", query="client/server span comparison", url=url, start=start, end=b.end),
                    observed_at=b.end, score=0.8))

    async def _canaries(self, b: ContextBundle) -> None:
        try:
            items = await self.cp.canaries(self.namespace)
        except UpstreamUnavailable:
            return
        for c in items:
            created = parse_ts(c.get("createdAt"))
            status = c.get("status", {})
            if created is None or created < b.onset - timedelta(minutes=15):
                continue
            b.canaries.append(c)
            last = (status.get("analyses") or [{}])[-1]
            b.add(f"canary:{c['name']}", Evidence(
                id=Evidence.make_id("canary", c["spec"]["targetRef"], c["name"]), kind=EvidenceKind.CANARY,
                service=c["spec"]["targetRef"], signal=f"canary.{status.get('phase', 'Pending')}",
                title=f"CanaryRelease {c['name']} ({c['spec']['release'].get('version')}) is {status.get('phase')}",
                summary=(f"Progressive release of {c['spec']['targetRef']} {c['spec']['release'].get('version')} is "
                         f"{status.get('phase')} at {status.get('effectiveWeight', 0)}% weight: {status.get('message', '')}; "
                         f"last analysis: {last.get('verdict', 'n/a')} {last.get('reason', '')}"),
                data={"phase": status.get("phase"), "weight": status.get("effectiveWeight"), "analyses": status.get("analyses", [])[-3:]},
                source=EvidenceSource(system="controlplane", query="GET /v1/canaries"),
                observed_at=created, score=0.9))

    # ------------------------------------------------------- derived facts --
    def _metric_evidence(self, b: ContextBundle) -> None:
        for name in b.scope:
            s = b.signals.get(name)
            if s is None:
                continue
            for a in s.anomalies:
                base = f" (baseline {a.baseline:.4g})" if a.baseline is not None else ""
                b.add(f"metric:{name}:{a.signal}", Evidence(
                    id=Evidence.make_id("metric", name, a.signal), kind=EvidenceKind.METRIC, service=name, signal=a.signal,
                    title=f"{name}: {a.reason}",
                    summary=f"{name} {a.signal}={a.value:.4g}{base} since {a.since:%H:%M:%S}Z: {a.reason}",
                    data={"value": a.value, "baseline": a.baseline, "threshold": a.threshold, "since": a.since.isoformat(),
                          "rps": s.rps, "errorRatio": s.error_ratio, "p95Ms": s.p95_ms, "cpuUtil": s.cpu_util,
                          "memUtil": s.mem_util, "dbPoolUtil": s.db_pool_util},
                    source=EvidenceSource(system="prometheus", query=f"detector signal {a.signal} for {name}", start=b.start, end=b.end),
                    observed_at=a.since, score=0.9 if a.signal in ("error_ratio", "crashloop", "oom_killed", "scaled_to_zero") else 0.8))
            if not s.anomalies and name in b.graph.nodes:
                b.add(f"healthy:{name}", Evidence(
                    id=Evidence.make_id("metric", name, "healthy"), kind=EvidenceKind.METRIC, service=name, signal="healthy",
                    title=f"{name} shows no anomalies",
                    summary=(f"{name} is healthy: {s.ready}/{s.desired} ready, 5xx {pct(s.error_ratio)}, p95 {ms(s.p95_ms)}, "
                             f"CPU {pct(s.cpu_util)} of limit, memory {pct(s.mem_util)} of limit"),
                    data={"ready": s.ready, "desired": s.desired, "errorRatio": s.error_ratio, "p95Ms": s.p95_ms,
                          "cpuUtil": s.cpu_util, "memUtil": s.mem_util},
                    source=EvidenceSource(system="prometheus", query=f"detector signals for {name}", start=b.start, end=b.end),
                    observed_at=b.end, score=0.35))
            if s.rps is not None and s.rps_baseline and name in b.anomalous:
                ratio = s.rps / s.rps_baseline
                if ratio >= 1.8 or ratio <= 0.3:
                    b.add(f"traffic:{name}", Evidence(
                        id=Evidence.make_id("metric", name, "traffic", f"{ratio:.0f}"), kind=EvidenceKind.METRIC,
                        service=name, signal="traffic_change",
                        title=f"{name} request rate {s.rps:.1f}/s is {ratio:.1f}x its baseline {s.rps_baseline:.1f}/s",
                        summary=(f"{name} is serving {s.rps:.2f} req/s versus a pre-incident median of "
                                 f"{s.rps_baseline:.2f} req/s ({ratio:.1f}x)"),
                        data={"rps": s.rps, "baseline": s.rps_baseline, "ratio": round(ratio, 2)},
                        source=EvidenceSource(system="prometheus", query=f"sum(rate(shop_http_requests_total{{service=\"{name}\"}}[30s]))",
                                              start=b.start, end=b.end),
                        observed_at=b.end, score=0.8))

    async def _replicaset_attribution(self, b: ContextBundle) -> None:
        for name in sorted(b.anomalous):
            q = (f'sum by (pod_template_hash) (rate(shop_http_requests_total{{namespace="{self.namespace}",service="{name}",code=~"5.."}}[1m])) '
                 f'/ sum by (pod_template_hash) (rate(shop_http_requests_total{{namespace="{self.namespace}",service="{name}"}}[1m]))')
            try:
                ratios = await self.prom.by_label(q, "pod_template_hash")
            except UpstreamUnavailable:
                return
            if len(ratios) < 1:
                continue
            b.rs_errors[name] = ratios
            w = b.workloads.get(name, {})
            revs = {r.get("podTemplateHash"): r for r in w.get("revisions", [])}
            if len(ratios) >= 2 or any(v > 0.05 for v in ratios.values()):
                desc = ", ".join(f"{h} (rev {revs.get(h, {}).get('revision', '?')}, {revs.get(h, {}).get('version') or 'n/a'}): {v:.1%}"
                                 for h, v in sorted(ratios.items(), key=lambda kv: -kv[1]))
                b.add(f"rs:{name}", Evidence(
                    id=Evidence.make_id("metric", name, "rs_errors", ",".join(sorted(ratios))), kind=EvidenceKind.METRIC,
                    service=name, signal="errors_by_replicaset", title=f"{name} 5xx ratio by ReplicaSet: {desc}"[:280],
                    summary=f"Error ratio of {name} split by pod-template-hash (ReplicaSet): {desc}",
                    data={"ratios": ratios, "revisions": {h: revs.get(h, {}).get("revision") for h in ratios}},
                    source=EvidenceSource(system="prometheus", query=q, start=b.start, end=b.end),
                    observed_at=b.end, score=0.8 if len(ratios) >= 2 else 0.5))

    async def _series(self, b: ContextBundle) -> None:
        """Attach compact time series to the top metric evidence (for charts and onset inspection)."""
        exprs = {
            "error_ratio": 'sum(rate(shop_http_requests_total{{namespace="{ns}",service="{s}",code=~"5.."}}[30s])) / '
                           'sum(rate(shop_http_requests_total{{namespace="{ns}",service="{s}"}}[30s]))',
            "latency_p95": 'histogram_quantile(0.95, sum by (le) (rate(shop_http_request_duration_seconds_bucket'
                           '{{namespace="{ns}",service="{s}"}}[30s]))) * 1000',
            "memory_pressure": 'max(container_memory_working_set_bytes{{namespace="{ns}",container="{s}"}}) / 1048576',
            "cpu_saturation": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container="{s}"}}[30s]))',
            "db_pool_saturation": 'sum(shop_db_pool_in_use{{namespace="{ns}",service="{s}"}})',
        }
        metric_evs = [e for e in b.evidence if e.kind == EvidenceKind.METRIC and e.signal in exprs][:8]
        start = max(b.onset - timedelta(minutes=5), b.end - timedelta(minutes=20))
        for e in metric_evs:
            q = exprs[e.signal or ""].format(ns=self.namespace, s=e.service)
            try:
                series = await self.prom.range(q, start, b.end, step="10s")
            except UpstreamUnavailable:
                return
            if series:
                pts = series[0]["points"]
                e.data["series"] = [[int(t), round(v, 4)] for t, v in pts[-90:]]
                e.data["seriesQuery"] = q

    def _topology_evidence(self, b: ContextBundle) -> None:
        roots = b.graph.root_candidates(b.anomalous)
        if not b.anomalous:
            return
        blast = sorted({u for r in roots for u in b.graph.upstream(r)})
        b.add("topology:roots", Evidence(
            id=Evidence.make_id("topology", ",".join(roots), "roots", ",".join(sorted(b.anomalous))), kind=EvidenceKind.TOPOLOGY,
            service=roots[0] if roots else None, signal="topology.root_candidates",
            title=f"Deepest anomalous services in the dependency graph: {', '.join(roots) or 'none'}",
            summary=(f"Anomalous: {', '.join(sorted(b.anomalous))}. Services with no anomalous dependency "
                     f"(root-cause candidates): {', '.join(roots) or 'none'}. Their upstream blast radius: {', '.join(blast) or 'none'}."),
            data={"anomalous": sorted(b.anomalous), "roots": roots, "blastRadius": blast,
                  "edges": [e.as_dict() for e in b.graph.edges.values()]},
            source=EvidenceSource(system="aegisops", query="dependency graph (declared + observed)"),
            observed_at=b.end, score=0.7))

    def _history_evidence(self, b: ContextBundle) -> None:
        for h in b.history[:3]:
            b.add(f"history:{h['id']}", Evidence(
                id=Evidence.make_id("history", h.get("root_service"), h["id"]), kind=EvidenceKind.HISTORY,
                service=h.get("root_service"), signal="history.prior_incident",
                title=f"Prior incident {h['id']}: {h.get('category')} on {h.get('root_service')} -> {h.get('outcome')}",
                summary=(f"{h['id']} ({h.get('detected_at')}) was diagnosed as {h.get('category')} on {h.get('root_service')}; "
                         f"outcome {h.get('outcome')}; actions: {', '.join(h.get('actions', [])) or 'none'}"),
                data=h, source=EvidenceSource(system="aegisops", query="incident history"),
                observed_at=b.end, score=0.3))
