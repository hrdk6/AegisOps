"""Log-signature clustering and trace blame analysis.

These reduce large volumes of raw telemetry to a handful of ranked, countable
facts, so the diagnosis (and any model) sees the smallest useful evidence set.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from aegis.security.redaction import sanitize_untrusted

_NORMALIZE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<id>"),
    (re.compile(r"'[^']{1,80}'"), "'<s>'"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|%)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def signature_of(message: str) -> str:
    sig = message
    for pattern, repl in _NORMALIZE:
        sig = pattern.sub(repl, sig)
    return sig.strip()[:200]


@dataclass
class LogSignature:
    service: str
    signature: str
    level: str
    count: int = 0
    first_ns: int = 0
    last_ns: int = 0
    sample: str = ""
    error_type: str | None = None
    trace_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"service": self.service, "signature": self.signature, "level": self.level, "count": self.count,
                "sample": self.sample, "errorType": self.error_type, "traceIds": self.trace_ids,
                "firstSeenNs": self.first_ns, "lastSeenNs": self.last_ns}


def cluster_logs(lines: list[dict[str, Any]], top: int = 12) -> list[LogSignature]:
    groups: dict[tuple[str, str], LogSignature] = {}
    for line in lines:
        f = line.get("fields") or {}
        service = f.get("service") or line.get("labels", {}).get("service_name", "unknown")
        level = str(f.get("level", "info")).lower()
        msg = str(f.get("msg", line.get("line", "")))
        if msg == "request completed":
            msg = f"{f.get('method', '')} {f.get('route', '')} -> {f.get('status', '')}"
        if f.get("error_type") and f["error_type"] not in msg:
            msg = f"{f['error_type']}: {msg}"
        sig = signature_of(msg)
        key = (service, sig)
        g = groups.get(key)
        if g is None:
            g = groups[key] = LogSignature(service=service, signature=sig, level=level,
                                           sample=sanitize_untrusted(msg, 300), error_type=f.get("error_type"))
        g.count += 1
        ts = int(line.get("ts", 0))
        g.first_ns = ts if g.first_ns == 0 else min(g.first_ns, ts)
        g.last_ns = max(g.last_ns, ts)
        tid = f.get("trace_id")
        if tid and len(g.trace_ids) < 3 and tid not in g.trace_ids:
            g.trace_ids.append(tid)
        if level in ("critical", "error") and g.level not in ("critical",):
            g.level = level
    ordered = sorted(groups.values(), key=lambda s: ({"critical": 0, "error": 1, "warning": 2}.get(s.level, 3), -s.count))
    return ordered[:top]


@dataclass
class TraceAnalysis:
    traces: int = 0
    error_traces: int = 0
    error_origins: Counter[str] = field(default_factory=Counter)  # "service op"
    error_origin_services: Counter[str] = field(default_factory=Counter)
    self_time_ms: Counter[str] = field(default_factory=Counter)  # service or service->datastore
    edge_gap_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    sample_trace_ids: list[str] = field(default_factory=list)

    def origin_fraction(self, service: str) -> float:
        return self.error_origin_services[service] / self.error_traces if self.error_traces else 0.0

    def self_time_fraction(self, key: str) -> float:
        total = sum(self.self_time_ms.values())
        return self.self_time_ms[key] / total if total else 0.0

    def mean_gap(self, edge: str) -> float | None:
        gaps = self.edge_gap_ms.get(edge)
        return sum(gaps) / len(gaps) if gaps else None

    def as_dict(self) -> dict[str, Any]:
        total = sum(self.self_time_ms.values()) or 1.0
        return {
            "traces": self.traces, "errorTraces": self.error_traces,
            "errorOrigins": dict(self.error_origins.most_common(5)),
            "errorOriginServices": {k: round(v / max(self.error_traces, 1), 3) for k, v in self.error_origin_services.most_common(5)},
            "selfTimeShare": {k: round(v / total, 3) for k, v in self.self_time_ms.most_common(6)},
            "edgeGapMs": {k: round(sum(v) / len(v), 1) for k, v in self.edge_gap_ms.items() if v},
            "sampleTraceIds": self.sample_trace_ids[:5],
        }


def _tag(span: dict[str, Any], key: str) -> Any:
    for t in span.get("tags", []):
        if t.get("key") == key:
            return t.get("value")
    return None


def _is_error(span: dict[str, Any]) -> bool:
    if _tag(span, "error") in (True, "true"):
        return True
    if _tag(span, "otel.status_code") == "ERROR":
        return True
    code = _tag(span, "http.response.status_code") or _tag(span, "http.status_code")
    try:
        return int(code) >= 500
    except (TypeError, ValueError):
        return False


def _datastore(span: dict[str, Any]) -> str | None:
    system = _tag(span, "db.system")
    if system in ("postgresql", "postgres"):
        return "postgres"
    if system == "redis":
        return "redis"
    return None


def _framework_span(span: dict[str, Any]) -> bool:
    """ASGI instrumentation emits internal "http send"/"http receive" spans that mirror the
    response status; they are not units of work and must not be blamed."""
    op = span.get("operationName", "")
    return _tag(span, "span.kind") == "internal" and (op.endswith(" http send") or op.endswith(" http receive"))


def analyze_traces(traces: list[dict[str, Any]], analysis: TraceAnalysis | None = None) -> TraceAnalysis:
    a = analysis or TraceAnalysis()
    for trace in traces:
        spans = [s for s in trace.get("spans", []) if not _framework_span(s)]
        procs = trace.get("processes", {})
        if not spans:
            continue
        a.traces += 1
        by_id = {s["spanID"]: s for s in spans}
        children: dict[str, list[dict[str, Any]]] = defaultdict(list)
        parent: dict[str, str] = {}
        for s in spans:
            for ref in s.get("references", []):
                if ref.get("refType") == "CHILD_OF" and ref.get("spanID") in by_id:
                    children[ref["spanID"]].append(s)
                    parent[s["spanID"]] = ref["spanID"]

        def depth(span_id: str, parents: dict[str, str] = parent) -> int:
            d = 0
            while span_id in parents and d < 256:
                span_id = parents[span_id]
                d += 1
            return d

        def service(s: dict[str, Any]) -> str:
            return procs.get(s.get("processID"), {}).get("serviceName", "unknown")  # noqa: B023 (same iteration)

        errors = [s for s in spans if _is_error(s)]
        if errors:
            a.error_traces += 1
            if len(a.sample_trace_ids) < 10:
                a.sample_trace_ids.append(trace.get("traceID", ""))
            # Origin: the deepest error span none of whose children errored.
            origins = [s for s in errors if not any(_is_error(c) for c in children.get(s["spanID"], []))]
            deepest = max(origins or errors, key=lambda s: (depth(s["spanID"]), s.get("startTime", 0)))
            svc = service(deepest)
            store = _datastore(deepest)
            origin = f"{svc}->{store}" if store else svc
            a.error_origins[f"{origin} {deepest.get('operationName', '')}"] += 1
            a.error_origin_services[origin] += 1
        for s in spans:
            kids = children.get(s["spanID"], [])
            self_ms = max(0.0, (s.get("duration", 0) - sum(c.get("duration", 0) for c in kids)) / 1000.0)
            store = _datastore(s)
            svc = service(s)
            a.self_time_ms[f"{svc}->{store}" if store else svc] += self_ms
            if _tag(s, "span.kind") == "client" and not store:
                for c in kids:
                    callee = service(c)
                    if callee != svc and _tag(c, "span.kind") == "server":
                        gap = (s.get("duration", 0) - c.get("duration", 0)) / 1000.0
                        a.edge_gap_ms[f"{svc}->{callee}"].append(max(0.0, gap))
    return a
