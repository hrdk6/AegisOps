"""Multi-signal anomaly detection.

Each cycle the detector gathers RED metrics (Prometheus), saturation
(cAdvisor), data-store pool signals and Kubernetes state (via the control
plane), then evaluates deterministic SLO rules with persistence. It keeps a
per-signal baseline that is only updated while a service is healthy, so an
ongoing incident cannot poison its own baseline.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aegis.clients.controlplane import ControlPlane
from aegis.clients.http import UpstreamUnavailable
from aegis.clients.prometheus import Prometheus
from aegis.domain.schemas import Anomaly, ServiceSignals
from aegis.engine.slo import SLOBook

log = logging.getLogger("aegis.detector")

WINDOW = "30s"
NS = 'namespace="{ns}"'

# name -> (grouping label, PromQL template)
QUERIES: dict[str, tuple[str, str]] = {
    "rps": ("service", 'sum by (service) (rate(shop_http_requests_total{{{ns}}}[{w}]))'),
    "errors": ("service", 'sum by (service) (rate(shop_http_requests_total{{{ns},code=~"5.."}}[{w}]))'),
    "p95": ("service", 'histogram_quantile(0.95, sum by (service, le) '
                       '(rate(shop_http_request_duration_seconds_bucket{{{ns}}}[{w}])))'),
    "cpu_used": ("container", 'sum by (container) (rate(container_cpu_usage_seconds_total{{{ns},container!=""}}[{w}]))'),
    "cpu_limit": ("container", 'sum by (container) (max by (namespace, pod, container) '
                              '(container_spec_cpu_quota{{{ns},container!=""}}) / on (namespace, pod, container) '
                              'max by (namespace, pod, container) (container_spec_cpu_period{{{ns},container!=""}}))'),
    # Both sides are reduced to one series per container instance first: cAdvisor briefly exposes two series
    # (old and new cgroup) for a restarted container, which would make the join many-to-many and fail.
    "mem_util": ("container", 'max by (container) (max by (namespace, pod, container) '
                             '(container_memory_working_set_bytes{{{ns},container!=""}}) / on (namespace, pod, container) '
                             'max by (namespace, pod, container) (container_spec_memory_limit_bytes{{{ns},container!=""}} > 0))'),
    "throttled": ("container", 'sum by (container) (rate(container_cpu_cfs_throttled_periods_total{{{ns},container!=""}}[{w}])) '
                              '/ sum by (container) (rate(container_cpu_cfs_periods_total{{{ns},container!=""}}[{w}]))'),
    "pool_util": ("service", 'sum by (service) (shop_db_pool_in_use{{{ns}}}) / sum by (service) (shop_db_pool_size{{{ns}}})'),
    "pool_waiting": ("service", 'sum by (service) (shop_db_pool_waiting{{{ns}}})'),
    "pool_timeouts": ("service", 'sum by (service) (rate(shop_db_acquire_timeouts_total{{{ns}}}[{w}]))'),
}

# Signals that are confirmed on first observation (state, not noisy rates).
IMMEDIATE = {"crashloop", "oom_killed", "scaled_to_zero"}
PERSIST_LONGER = {"unavailable_replicas": 3, "traffic_drop": 3}


@dataclass
class Observation:
    """Raw inputs for one detection cycle (pure data; see compute_signals)."""

    at: datetime
    metrics: dict[str, dict[str, float]]
    workloads: list[dict[str, Any]]


@dataclass
class DetectorState:
    baselines: dict[tuple[str, str], deque[float]] = field(default_factory=dict)
    streaks: dict[tuple[str, str], int] = field(default_factory=dict)
    first_seen: dict[tuple[str, str], datetime] = field(default_factory=dict)
    desired_baseline: dict[str, int] = field(default_factory=dict)

    def baseline(self, service: str, signal: str) -> float | None:
        values = self.baselines.get((service, signal))
        if not values or len(values) < 6:
            return None
        return statistics.median(values)

    def record_baseline(self, service: str, signal: str, value: float | None) -> None:
        if value is None:
            return
        self.baselines.setdefault((service, signal), deque(maxlen=120)).append(value)


def _pods_summary(w: dict[str, Any], at: datetime) -> tuple[int, int, int]:
    """(restarts in last 3 min, OOMKilled in last 3 min, crash-looping pods)."""
    restarts = ooms = crash = 0
    horizon = at - timedelta(minutes=3)
    for p in w.get("pods", []):
        if p.get("quarantined"):
            continue
        term = p.get("lastTerminationAt")
        recent = False
        if term:
            try:
                recent = datetime.fromisoformat(term.replace("Z", "+00:00")) >= horizon
            except ValueError:
                recent = False
        if recent:
            restarts += 1
            if p.get("lastTerminationReason") == "OOMKilled":
                ooms += 1
        if p.get("waitingReason") in ("CrashLoopBackOff", "CreateContainerConfigError", "ImagePullBackOff",
                                      "ErrImagePull", "RunContainerError"):
            crash += 1
    return restarts, ooms, crash


def compute_signals(obs: Observation, slos: SLOBook, state: DetectorState, persistence: int = 2) -> dict[str, ServiceSignals]:
    """Pure detection step: raw observation + state → per-service signals."""
    m = obs.metrics
    out: dict[str, ServiceSignals] = {}
    container_to_workload = {}
    for w in obs.workloads:
        for c in w.get("containers", []):
            container_to_workload[c["name"]] = w["name"]

    def by_workload(metric: str) -> dict[str, float]:
        res: dict[str, float] = {}
        for container, v in m.get(metric, {}).items():
            wl = container_to_workload.get(container, container)
            res[wl] = max(res.get(wl, v), v)
        return res

    cpu_used, cpu_limit = by_workload("cpu_used"), by_workload("cpu_limit")
    mem_util, throttled = by_workload("mem_util"), by_workload("throttled")
    present: set[tuple[str, str]] = set()

    for w in obs.workloads:
        name = w["name"]
        if w.get("labels", {}).get("track") == "canary" or name.endswith("-canary"):
            continue
        slo = slos.get(name)
        is_http = name in slos.services
        s = ServiceSignals(service=name, at=obs.at, ready=w.get("readyReplicas", 0), desired=w.get("replicas", 0))
        restarts, ooms, crash = _pods_summary(w, obs.at)
        s.restarts_recent, s.oom_recent, s.crashloop_pods = restarts, ooms, crash
        if name in cpu_used and cpu_limit.get(name):
            s.cpu_util = round(cpu_used[name] / cpu_limit[name], 4)
        s.mem_util = round(mem_util[name], 4) if name in mem_util else None
        s.throttle_ratio = round(throttled[name], 4) if name in throttled else None
        if is_http:
            s.rps_baseline = state.baseline(name, "rps")
            s.p95_baseline = state.baseline(name, "p95_ms")
            rps = m.get("rps", {}).get(name)
            s.rps = round(rps, 4) if rps is not None else None
            if rps and rps > 0:
                s.error_ratio = round(m.get("errors", {}).get(name, 0.0) / rps, 4)
            p95 = m.get("p95", {}).get(name)
            s.p95_ms = round(p95 * 1000, 1) if p95 is not None else None
            if name in m.get("pool_util", {}):
                s.db_pool_util = round(m["pool_util"][name], 4)
                s.db_waiting = m.get("pool_waiting", {}).get(name, 0.0)
                s.db_timeouts_per_s = round(m.get("pool_timeouts", {}).get(name, 0.0), 4)

        candidates: list[Anomaly] = []

        # Closure is invoked only within this loop iteration.
        def flag(signal: str, value: float, reason: str, threshold: float | None = None) -> None:  # noqa: B023
            candidates.append(Anomaly(signal=signal, value=value, threshold=threshold,  # noqa: B023
                                      baseline=state.baseline(name, signal), reason=reason, since=obs.at))  # noqa: B023

        active = s.rps is not None and s.rps >= slo.min_rps
        if is_http and active and s.error_ratio is not None and s.error_ratio > slo.max_error_ratio:
            flag("error_ratio", s.error_ratio, f"5xx ratio {s.error_ratio:.1%} exceeds SLO {slo.max_error_ratio:.1%}",
                 slo.max_error_ratio)
        if is_http and active and s.p95_ms is not None:
            base = state.baseline(name, "p95_ms")
            if s.p95_ms > slo.p95_ms:
                flag("latency_p95", s.p95_ms, f"p95 {s.p95_ms:.0f}ms exceeds SLO {slo.p95_ms:.0f}ms", slo.p95_ms)
            elif base and s.p95_ms > 3 * base and s.p95_ms - base > 150:
                flag("latency_p95", s.p95_ms, f"p95 {s.p95_ms:.0f}ms is {s.p95_ms / base:.1f}x baseline {base:.0f}ms")
        if s.cpu_util is not None and s.cpu_util > slo.cpu_util:
            flag("cpu_saturation", s.cpu_util, f"CPU at {s.cpu_util:.0%} of limit", slo.cpu_util)
        if s.throttle_ratio is not None and s.throttle_ratio > slo.throttle_ratio and (s.cpu_util or 0) > 0.5:
            flag("cpu_throttling", s.throttle_ratio, f"{s.throttle_ratio:.0%} of CFS periods throttled", slo.throttle_ratio)
        if s.mem_util is not None and s.mem_util > slo.mem_util:
            flag("memory_pressure", s.mem_util, f"memory at {s.mem_util:.0%} of limit", slo.mem_util)
        if s.db_pool_util is not None and s.db_pool_util >= 0.95 and ((s.db_waiting or 0) > 0 or (s.db_timeouts_per_s or 0) > 0):
            flag("db_pool_saturation", s.db_pool_util,
                 f"DB pool {s.db_pool_util:.0%} in use, {s.db_waiting or 0:.0f} waiting, "
                 f"{s.db_timeouts_per_s or 0:.2f} acquire timeouts/s")
        if ooms:
            flag("oom_killed", float(ooms), f"{ooms} container(s) OOMKilled in the last 3 minutes")
        if crash:
            flag("crashloop", float(crash), f"{crash} pod(s) failing to start (CrashLoopBackOff)")
        prior = state.desired_baseline.get(name, 0)
        if s.desired == 0 and prior > 0:
            flag("scaled_to_zero", 0.0, f"desired replicas 0 (normally {prior})")
        elif s.desired > 0 and s.ready < s.desired:
            flag("unavailable_replicas", float(s.desired - s.ready), f"{s.ready}/{s.desired} replicas ready")
        rps_base = state.baseline(name, "rps")
        # Only when the rate query succeeded: an unavailable metric is unknown, not "zero traffic".
        if is_http and "rps" in m and rps_base and rps_base > 1.0 and (s.rps or 0) < 0.2 * rps_base:
            flag("traffic_drop", s.rps or 0.0, f"request rate {s.rps or 0:.2f}/s vs baseline {rps_base:.2f}/s")

        confirmed: list[Anomaly] = []
        for a in candidates:
            key = (name, a.signal)
            present.add(key)
            state.streaks[key] = state.streaks.get(key, 0) + 1
            state.first_seen.setdefault(key, obs.at)
            need = 1 if a.signal in IMMEDIATE else PERSIST_LONGER.get(a.signal, persistence)
            if state.streaks[key] >= need:
                a.since = state.first_seen[key]
                confirmed.append(a)
        s.anomalies = confirmed
        s.pending = [a for a in candidates if a not in confirmed]

        down = (s.desired > 0 and s.ready == 0) or any(a.signal == "scaled_to_zero" for a in confirmed) or (
            s.error_ratio is not None and active and s.error_ratio >= 0.5)
        if down and confirmed:
            s.status = "down"
        elif confirmed:
            s.status = "degraded"
        elif is_http and s.rps is None and s.desired > 0:
            s.status = "unknown" if s.ready == 0 else "healthy"
        else:
            s.status = "healthy"

        if s.status == "healthy" and not candidates:
            for sig, val in (("rps", s.rps), ("error_ratio", s.error_ratio), ("p95_ms", s.p95_ms),
                             ("cpu_util", s.cpu_util), ("mem_util", s.mem_util)):
                state.record_baseline(name, sig, val)
            if s.desired > 0:
                state.desired_baseline[name] = s.desired
        out[name] = s

    for key in list(state.streaks):
        if key not in present:
            state.streaks.pop(key, None)
            state.first_seen.pop(key, None)
    return out


class Detector:
    """I/O wrapper around compute_signals."""

    def __init__(self, prom: Prometheus, cp: ControlPlane, slos: SLOBook, namespace: str, persistence: int = 2) -> None:
        self.prom = prom
        self.cp = cp
        self.slos = slos
        self.namespace = namespace
        self.persistence = persistence
        self.state = DetectorState()
        self.latest: dict[str, ServiceSignals] = {}
        self.history: deque[dict[str, ServiceSignals]] = deque(maxlen=180)
        self.workloads: list[dict[str, Any]] = []
        self.degraded_sources: set[str] = set()
        self._cycle = asyncio.Event()

    async def next_cycle(self, timeout: float = 30.0) -> bool:
        """Wait for the next completed detection cycle (event-driven consumers)."""
        event = self._cycle
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _metrics(self) -> dict[str, dict[str, float]]:
        """Run every query independently: one failing query (e.g. a PromQL matching error during a pod
        restart) must not blind the detector to every other signal. Signals whose query failed are simply
        absent this cycle, which the rules treat as unknown, never as zero."""
        ns = NS.format(ns=self.namespace)
        out: dict[str, dict[str, float]] = {}
        failed: dict[str, str] = {}
        for key, (label, tmpl) in QUERIES.items():
            try:
                out[key] = await self.prom.by_label(tmpl.format(ns=ns, w=WINDOW), label)
            except UpstreamUnavailable as exc:
                failed[key] = exc.detail
                if not out and len(failed) >= 2:
                    break  # Prometheus itself is down; do not wait for every query to time out
        if failed:
            log.warning("prometheus queries failed; affected signals skipped",
                        extra={"fields": {"queries": sorted(failed), "error": next(iter(failed.values()))[:300]}})
            self.degraded_sources.add("prometheus" if not out else "prometheus:" + ",".join(sorted(failed)))
        return out

    async def observe(self) -> Observation:
        now = datetime.now(UTC)
        self.degraded_sources.clear()
        # Fail safe: missing metrics disable metric-based detection; K8s signals are still evaluated.
        metrics = await self._metrics()
        try:
            self.workloads = await self.cp.workloads(self.namespace)
        except UpstreamUnavailable as exc:
            log.warning("control plane unavailable; kubernetes signals skipped", extra={"fields": {"error": exc.detail}})
            self.degraded_sources.add("controlplane")
        return Observation(at=now, metrics=metrics, workloads=self.workloads)

    async def evaluate(self) -> dict[str, ServiceSignals]:
        obs = await self.observe()
        signals = compute_signals(obs, self.slos, self.state, self.persistence)
        self.latest = signals
        self.history.append(signals)
        done, self._cycle = self._cycle, asyncio.Event()
        done.set()
        return signals

    def recent(self, seconds: float) -> list[dict[str, ServiceSignals]]:
        cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
        return [h for h in self.history if h and next(iter(h.values())).at >= cutoff]
