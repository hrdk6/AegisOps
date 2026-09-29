"""Deterministic, evidence-weighted hypothesis generation.

Each rule inspects the ContextBundle for a failure *mechanism* or *trigger*
and returns a scored candidate that cites the evidence supporting it and the
evidence contradicting it. Scores are additive log-odds-style weights; the
diagnosis engine converts them to confidences with a tempered softmax that
includes an UNKNOWN alternative, so weak evidence yields low confidence.

Rules are generic: they reason about changes, symptoms, topology and
telemetry shapes. They never key on service names or on the specific flags a
scenario uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aegis.domain.enums import CauseCategory
from aegis.engine.context import ContextBundle, parse_ts
from aegis.engine.quantity import parse_quantity

DATASTORES = {"postgres", "redis"}
CONFIG_WORDS = re.compile(r"config|invalid|parse|unsupported|missing required|validation", re.I)
POOL_WORDS = re.compile(r"pool exhausted|PoolExhausted|acquir", re.I)


@dataclass
class Candidate:
    category: CauseCategory
    component: str
    score: float
    statement: str
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    trigger_change: str | None = None
    edge: str | None = None
    factors: list[str] = field(default_factory=list)

    def plus(self, points: float, why: str, *evidence: str | None) -> None:
        self.score += points
        self.factors.append(f"{points:+.1f} {why}")
        for e in evidence:
            if e and e not in self.supporting:
                self.supporting.append(e)

    def minus(self, points: float, why: str, *evidence: str | None) -> None:
        self.score -= points
        self.factors.append(f"-{points:.1f} {why}")
        for e in evidence:
            if e and e not in self.contradicting:
                self.contradicting.append(e)


# ----------------------------------------------------------------- helpers --


def signals_of(b: ContextBundle, svc: str) -> set[str]:
    s = b.signals.get(svc)
    return {a.signal for a in s.anomalies} if s else set()


def pod_failures(b: ContextBundle, svc: str) -> bool:
    s = b.signals.get(svc)
    return bool(s and (s.crashloop_pods or s.oom_recent)) or b.ev(f"k8s:{svc}:pods") is not None


def symptomatic(b: ContextBundle, svc: str) -> bool:
    return svc in b.anomalous or pod_failures(b, svc)


def is_root(b: ContextBundle, svc: str) -> bool:
    return svc in b.graph.root_candidates(b.anomalous)


def proximity(b: ContextBundle, svc: str, change: dict[str, Any]) -> tuple[float, float]:
    """(points, seconds before onset) for a change relative to svc's onset."""
    t = parse_ts(change.get("time"))
    if t is None:
        return 0.0, 0.0
    delta = (b.onset_of(svc) - t).total_seconds()
    if delta < -90:
        return -1.0, delta
    if delta < 0:
        return 1.0, delta
    if delta <= 180:
        return 2.0, delta
    if delta <= 600:
        return 1.2, delta
    return 0.6, delta


def change_ev(b: ContextBundle, change: dict[str, Any]) -> str | None:
    return b.ev(f"change:{change['id']}")


def symptom_evs(b: ContextBundle, svc: str) -> list[str]:
    out = [b.ev(f"metric:{svc}:{sig}") for sig in sorted(signals_of(b, svc))]
    return [e for e in out if e]


def _paths(change: dict[str, Any]) -> list[str]:
    return [f.get("path", "") for f in change.get("fields", [])]


def is_deploy_change(c: dict[str, Any]) -> bool:
    return c.get("kind") == "Deployment" and c.get("type") in ("release", "env", "template")


def is_resource_change(c: dict[str, Any]) -> bool:
    return c.get("kind") == "Deployment" and any(".resources." in p for p in _paths(c))


def is_scale_change(c: dict[str, Any]) -> bool:
    return c.get("kind") == "Deployment" and "spec.replicas" in _paths(c)


def describe_change(c: dict[str, Any], limit: int = 3) -> str:
    parts = [f"{f['path'].split('.')[-1]} {f.get('old') or '<none>'}→{f.get('new') or '<none>'}"
             for f in c.get("fields", [])[:limit]]
    return ", ".join(parts) or c.get("type", "change")


def earlier_dependency_root(b: ContextBundle, svc: str) -> str | None:
    """An anomalous dependency of svc whose onset precedes svc's by >20s."""
    mine = b.onset_of(svc)
    for dep in b.graph.downstream(svc):
        if dep in b.anomalous and (mine - b.onset_of(dep)).total_seconds() > 20 and is_root(b, dep):
            return dep
    return None


# ------------------------------------------------------------------- rules --


def rule_bad_deployment(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        changes = [c for c in b.changes_for(svc) if is_deploy_change(c) and not is_resource_change(c)]
        if not changes:
            continue
        c = changes[0]
        pts, delta = proximity(b, svc, c)
        origin = b.traces.origin_fraction(svc)
        if not symptomatic(b, svc) and origin < 0.5:
            continue
        cand = Candidate(CauseCategory.BAD_DEPLOYMENT, svc, 3.0,
                         f"Deployment change to {svc} ({describe_change(c)}) {abs(delta):.0f}s "
                         f"{'before' if delta >= 0 else 'after'} anomaly onset introduced a regression",
                         trigger_change=change_ev(b, c))
        cand.plus(pts, "temporal proximity of change to onset", change_ev(b, c), *symptom_evs(b, svc))
        if is_root(b, svc):
            cand.plus(1.5, "deepest anomalous service in the dependency graph", b.ev("topology:roots"))
        if origin >= 0.5:
            cand.plus(1.5, f"{origin:.0%} of failing traces originate here", b.ev("trace:error_origin"))
        if "latency_p95" in signals_of(b, svc) and b.traces.self_time_fraction(svc) >= 0.4:
            cand.plus(1.0, "latency attributed to this service's own spans", b.ev("trace:self_time"))
        ratios = b.rs_errors.get(svc, {})
        revs = {r.get("podTemplateHash"): r.get("revision", 0) for r in b.workloads.get(svc, {}).get("revisions", [])}
        if len(ratios) >= 2:
            newest = max(ratios, key=lambda h: revs.get(h) or 0)
            if ratios[newest] - min(ratios.values()) > 0.05:
                cand.plus(1.5, "errors concentrated in the newest ReplicaSet", b.ev(f"rs:{svc}"))
        if pod_failures(b, svc):
            cand.plus(1.0, "new pods failing", b.ev(f"k8s:{svc}:pods"))
        if delta < -60:
            cand.minus(3.0, "symptoms began before the change", *symptom_evs(b, svc))
        if dep := earlier_dependency_root(b, svc):
            cand.minus(1.5, f"dependency {dep} degraded earlier", *symptom_evs(b, dep))
        out.append(cand)
    return out


def rule_config_error(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        if not symptomatic(b, svc):
            continue
        changes = b.changes_for(svc)
        cm = [c for c in changes if c.get("kind") == "ConfigMap"]
        env = [c for c in changes if is_deploy_change(c)]
        logs = [s for s in b.log_signatures(svc) if s.level in ("critical", "error") and CONFIG_WORDS.search(s.signature)]
        if not (cm or (env and logs)):
            continue
        trigger = cm[0] if cm else env[0]
        pts, delta = proximity(b, svc, trigger)
        what = f"ConfigMap {trigger['name']}" if cm else f"environment of {svc}"
        cand = Candidate(CauseCategory.CONFIG_ERROR, svc, 3.0,
                         f"Configuration change to {what} ({describe_change(trigger, 2)}) left {svc} with invalid "
                         f"configuration", trigger_change=change_ev(b, trigger))
        cand.plus(pts + (1.0 if cm else 0.0), "configuration changed shortly before onset", change_ev(b, trigger))
        if pod_failures(b, svc) or "crashloop" in signals_of(b, svc):
            cand.plus(2.0, "pods failing to start", b.ev(f"k8s:{svc}:pods"), b.ev(f"metric:{svc}:crashloop"))
        if logs:
            cand.plus(2.0, "configuration validation errors in logs", b.ev(f"log:{svc}:{logs[0].signature}"))
        if is_root(b, svc):
            cand.plus(1.0, "deepest anomalous service", b.ev("topology:roots"))
        if pts < 0:
            cand.minus(2.0, "configuration changed after symptoms began", change_ev(b, trigger))
        out.append(cand)
    return out


def rule_resource_misconfiguration(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        changes = [c for c in b.changes_for(svc) if is_resource_change(c)]
        if not changes or not symptomatic(b, svc):
            continue
        c = changes[0]
        pts, delta = proximity(b, svc, c)
        mem_down = cpu_down = False
        for f in c.get("fields", []):
            old, new = parse_quantity(f.get("old")), parse_quantity(f.get("new"))
            if old and new and new < old:
                if f["path"].endswith("memory"):
                    mem_down = True
                if f["path"].endswith("cpu"):
                    cpu_down = True
        sigs = signals_of(b, svc)
        cand = Candidate(CauseCategory.RESOURCE_MISCONFIGURATION, svc, 3.5,
                         f"Resource settings of {svc} were reduced ({describe_change(c, 4)}), below what the workload needs",
                         trigger_change=change_ev(b, c))
        cand.plus(pts, "resource change shortly before onset", change_ev(b, c))
        if mem_down and ({"oom_killed", "memory_pressure", "crashloop"} & sigs or pod_failures(b, svc)):
            cand.plus(2.5, "memory limit lowered and containers are OOMKilled / under memory pressure",
                      b.ev(f"metric:{svc}:oom_killed"), b.ev(f"k8s:{svc}:pods"), b.ev(f"metric:{svc}:memory_pressure"))
        elif cpu_down and {"cpu_throttling", "cpu_saturation", "latency_p95"} & sigs:
            cand.plus(2.5, "CPU limit lowered and the service is throttled / slow",
                      b.ev(f"metric:{svc}:cpu_throttling"), b.ev(f"metric:{svc}:cpu_saturation"), b.ev(f"metric:{svc}:latency_p95"))
        else:
            cand.minus(1.5, "no symptom matching the reduced resource")
        out.append(cand)
    return out


def rule_cpu_saturation(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        sigs = signals_of(b, svc)
        if not {"cpu_saturation", "cpu_throttling"} & sigs:
            continue
        s = b.signals[svc]
        cand = Candidate(CauseCategory.CPU_SATURATION, svc, 2.5,
                         f"{svc} is CPU-saturated ({(s.cpu_util or 0):.0%} of its CPU limit), queueing requests",
                         supporting=[e for e in (b.ev(f"metric:{svc}:cpu_saturation"), b.ev(f"metric:{svc}:cpu_throttling")) if e])
        if "latency_p95" in sigs or any("latency_p95" in signals_of(b, d) for d in b.graph.upstream(svc)):
            cand.plus(1.0, "latency elevated at or above the saturated service", b.ev(f"metric:{svc}:latency_p95"))
        traffic = b.by_id(b.ev(f"traffic:{svc}") or "")
        if traffic and traffic.data.get("ratio", 1) >= 1.8:
            cand.plus(1.5, "request rate far above baseline (traffic surge)", traffic.id)
            cand.statement = (f"Traffic to {svc} rose to {traffic.data['ratio']:.1f}x its baseline and the service "
                              f"saturated its CPU ({(s.cpu_util or 0):.0%} of limit)")
        if is_root(b, svc):
            cand.plus(1.0, "deepest anomalous service", b.ev("topology:roots"))
        if b.traces.self_time_fraction(svc) >= 0.4:
            cand.plus(1.0, "request time dominated by this service's own spans", b.ev("trace:self_time"))
        deploy = [c for c in b.changes_for(svc) if is_deploy_change(c)]
        if deploy:
            cand.trigger_change = change_ev(b, deploy[0])
            cand.minus(1.0, "a recent deployment may explain the saturation", change_ev(b, deploy[0]))
        if any(is_resource_change(c) for c in b.changes_for(svc)):
            cand.minus(1.5, "a recent resource change may explain the saturation")
        out.append(cand)
    return out


def rule_memory_exhaustion(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        sigs = signals_of(b, svc)
        s = b.signals.get(svc)
        pods = b.by_id(b.ev(f"k8s:{svc}:pods") or "")
        oom = "oom_killed" in sigs or (pods is not None and "OOMKilled" in pods.data.get("reasons", []))
        if not (oom or "memory_pressure" in sigs):
            continue
        cand = Candidate(CauseCategory.MEMORY_EXHAUSTION, svc, 2.5,
                         f"{svc} is exhausting its memory limit" + (" and being OOMKilled" if oom else ""))
        if oom:
            cand.plus(2.0, "containers OOMKilled", b.ev(f"metric:{svc}:oom_killed"), b.ev(f"k8s:{svc}:pods"),
                      b.ev(f"event:{svc}:OOMKilled"))
        if "memory_pressure" in sigs:
            cand.plus(1.0, f"working set at {(s.mem_util or 0) if s else 0:.0%} of limit", b.ev(f"metric:{svc}:memory_pressure"))
        deploy = [c for c in b.changes_for(svc) if is_deploy_change(c)]
        if deploy:
            cand.trigger_change = change_ev(b, deploy[0])
            cand.plus(0.5, "memory growth began after a deployment", change_ev(b, deploy[0]))
        res = [c for c in b.changes_for(svc) if is_resource_change(c)]
        if res:
            cand.minus(1.0, "a recent resource change may explain the OOMs", change_ev(b, res[0]))
        out.append(cand)
    return out


def rule_db_connection_exhaustion(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        if "db_pool_saturation" not in signals_of(b, svc):
            continue
        cand = Candidate(CauseCategory.DB_CONNECTION_EXHAUSTION, svc, 3.5,
                         f"{svc}'s database connection pool is exhausted; requests wait for or time out acquiring connections",
                         supporting=[e for e in [b.ev(f"metric:{svc}:db_pool_saturation")] if e])
        logs = [s for s in b.log_signatures(svc) if POOL_WORDS.search(s.signature)]
        if logs:
            cand.plus(2.0, "pool exhaustion errors in logs", b.ev(f"log:{svc}:{logs[0].signature}"))
        pg = b.signals.get("postgres")
        if pg and not pg.anomalies and pg.ready >= 1:
            cand.plus(1.0, "database server itself is healthy", b.ev("healthy:postgres"))
        others_ok = [e for e, m in b.edge_metrics.items() if e.endswith("->postgres") and not e.startswith(svc)
                     and (m.get("p95_ms") or 0) < 100]
        if others_ok:
            cand.plus(0.5, "other services' database calls are fast")
        deploy = [c for c in b.changes_for(svc) if is_deploy_change(c)]
        if deploy:
            cand.trigger_change = change_ev(b, deploy[0])
            cand.plus(0.5, "pool exhaustion began after a deployment", change_ev(b, deploy[0]))
        out.append(cand)
    return out


def rule_dependency_failure(b: ContextBundle) -> list[Candidate]:
    out = []
    for dep in b.scope:
        s = b.signals.get(dep)
        if s is None:
            continue
        sigs = signals_of(b, dep)
        down = "scaled_to_zero" in sigs or (s.desired > 0 and s.ready == 0)
        if not down:
            continue
        callers = sorted(set(b.graph.dependents(dep)) & b.anomalous)
        cand = Candidate(CauseCategory.DEPENDENCY_FAILURE, dep, 4.0,
                         f"{dep} is unavailable ({s.ready}/{s.desired} ready); dependent services "
                         f"{', '.join(callers) or 'n/a'} fail calling it",
                         supporting=[e for e in (b.ev(f"metric:{dep}:scaled_to_zero"), b.ev(f"k8s:{dep}:rollout"),
                                                 b.ev(f"metric:{dep}:unavailable_replicas")) if e])
        if callers:
            cand.plus(1.0, f"{len(callers)} dependent service(s) degraded", *[x for c in callers for x in symptom_evs(b, c)][:4])
        mentions = [sig for c in callers for sig in b.log_signatures(c) if dep.split("-")[0] in sig.signature.lower()]
        if mentions:
            cand.plus(1.5, f"callers log failures reaching {dep}", b.ev(f"log:{mentions[0].service}:{mentions[0].signature}"))
        for e, m in b.edge_metrics.items():
            if e.endswith(f"->{dep}") and m.get("error_ratio", 0) >= 0.05:
                cand.plus(0.5, f"client-side errors on {e}", b.ev(f"edge:{e}"))
                break
        scale = [c for c in b.changes_for(dep) if is_scale_change(c)]
        if scale:
            cand.trigger_change = change_ev(b, scale[0])
            cand.plus(1.5, f"{dep} was scaled ({describe_change(scale[0])})", change_ev(b, scale[0]))
        out.append(cand)
    return out


def rule_network_degradation(b: ContextBundle) -> list[Candidate]:
    out = []
    for edge, m in b.edge_metrics.items():
        caller, dep = edge.split("->", 1)
        if caller not in b.anomalous:
            continue
        ds = b.signals.get(dep)
        dep_down = ds is not None and ((ds.desired > 0 and ds.ready == 0) or "scaled_to_zero" in signals_of(b, dep))
        if dep_down:
            continue
        client_p95 = m.get("p95_ms") or 0.0
        err = m.get("error_ratio", 0.0)
        if dep in DATASTORES:
            peers = [pm.get("p95_ms") or 0 for e, pm in b.edge_metrics.items() if e.endswith(f"->{dep}") and e != edge]
            healthy_peer = bool(peers) and min(peers) < max(50.0, client_p95 / 4)
            latency_div = client_p95 > 200 and (healthy_peer or not peers)
            server_ok = healthy_peer
        else:
            server_p95 = (ds.p95_ms if ds else None) or 0.0
            server_err = (ds.error_ratio if ds else 0.0) or 0.0
            latency_div = client_p95 > 200 and client_p95 > 2.5 * server_p95 + 100
            server_ok = server_err < 0.02
        error_div = err >= 0.05 and server_ok
        if not (latency_div or error_div):
            continue
        cand = Candidate(CauseCategory.NETWORK_DEGRADATION, dep, 3.0,
                         f"The network path {caller} -> {dep} is degraded: client-side "
                         f"{'p95 ' + format(client_p95, '.0f') + 'ms' if latency_div else ''}"
                         f"{' and ' if latency_div and error_div else ''}"
                         f"{'errors ' + format(err, '.0%') if error_div else ''} while {dep} itself is healthy",
                         edge=edge, supporting=[e for e in [b.ev(f"edge:{edge}")] if e])
        if latency_div:
            cand.plus(2.0, "client-side latency far above server-side latency / peer callers")
        if error_div:
            cand.plus(2.0, "client-side failures while the dependency reports no errors")
        if b.ev(f"trace:gap:{edge}"):
            cand.plus(1.5, "traces show time spent between client and server spans", b.ev(f"trace:gap:{edge}"))
        if b.ev(f"healthy:{dep}"):
            cand.plus(1.0, f"{dep} shows no anomalies", b.ev(f"healthy:{dep}"))
        if not b.changes_for(caller) and not b.changes_for(dep):
            cand.plus(0.5, "no recent changes to either side")
        if dep in b.anomalous:
            cand.minus(2.0, f"{dep} is itself anomalous", *symptom_evs(b, dep))
        if ds is not None and (ds.error_ratio or 0) >= 0.02:
            cand.minus(2.5, f"{dep} reports server-side errors: the failure is in the service, not the path",
                       b.ev(f"metric:{dep}:error_ratio"))
        redeploys = [c for c in b.changes_for(dep) if is_deploy_change(c)]
        if redeploys:
            cand.minus(2.0, f"{dep} was redeployed around onset (connection churn, not a network fault)",
                       change_ev(b, redeploys[0]))
        out.append(cand)
    return out


def rule_crashloop(b: ContextBundle) -> list[Candidate]:
    out = []
    for svc in b.scope:
        if "crashloop" in signals_of(b, svc):
            out.append(Candidate(CauseCategory.POD_CRASHLOOP, svc, 2.5, f"{svc} pods are crash-looping",
                                 supporting=[e for e in (b.ev(f"metric:{svc}:crashloop"), b.ev(f"k8s:{svc}:pods")) if e]))
    return out


def rule_canary(b: ContextBundle) -> list[Candidate]:
    out = []
    for c in b.canaries:
        svc = c["spec"]["targetRef"]
        analyses = c.get("status", {}).get("analyses", [])
        failed = [a for a in analyses if a.get("verdict") == "Fail"]
        canary_err = max((a.get("canaryErrorRate", 0) for a in analyses), default=0.0)
        stable_err = max((a.get("stableErrorRate", 0) for a in analyses), default=0.0)
        ratios = b.rs_errors.get(svc, {})
        if not failed and canary_err <= stable_err + 0.02 and not ratios:
            continue
        cand = Candidate(CauseCategory.BAD_DEPLOYMENT, svc, 3.0,
                         f"Canary release {c['spec']['release'].get('version')} of {svc} fails its SLO gates "
                         f"(canary errors {canary_err:.1%} vs stable {stable_err:.1%})",
                         supporting=[e for e in [b.ev(f"canary:{c['name']}")] if e])
        if failed:
            cand.plus(3.0, "canary analysis failed", b.ev(f"canary:{c['name']}"))
        elif canary_err > stable_err + 0.02:
            cand.plus(2.0, "canary error rate above stable", b.ev(f"canary:{c['name']}"))
        if is_root(b, svc):
            cand.plus(1.0, "deepest anomalous service", b.ev("topology:roots"))
        out.append(cand)
    return out


RULES = [rule_bad_deployment, rule_config_error, rule_resource_misconfiguration, rule_cpu_saturation,
         rule_memory_exhaustion, rule_db_connection_exhaustion, rule_dependency_failure, rule_network_degradation,
         rule_crashloop, rule_canary]

MECHANISMS = {CauseCategory.MEMORY_EXHAUSTION, CauseCategory.CPU_SATURATION, CauseCategory.DB_CONNECTION_EXHAUSTION,
              CauseCategory.POD_CRASHLOOP}
TRIGGERS = {CauseCategory.BAD_DEPLOYMENT, CauseCategory.CONFIG_ERROR, CauseCategory.RESOURCE_MISCONFIGURATION}


def generate(b: ContextBundle) -> list[Candidate]:
    merged: dict[tuple[CauseCategory, str], Candidate] = {}
    for rule in RULES:
        for cand in rule(b):
            key = (cand.category, cand.component)
            prev = merged.get(key)
            if prev is None or cand.score > prev.score:
                if prev:
                    cand.supporting += [e for e in prev.supporting if e not in cand.supporting]
                merged[key] = cand
    cands = list(merged.values())
    # Link mechanisms to their triggers on the same component.
    for m in cands:
        if m.category in MECHANISMS and m.trigger_change is None:
            t = next((c for c in cands if c.category in TRIGGERS and c.component == m.component and c.trigger_change), None)
            if t:
                m.trigger_change = t.trigger_change
    return sorted(cands, key=lambda c: c.score, reverse=True)

