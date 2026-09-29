"""Service health, metrics (fixed queries only), changes and the dependency graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.api.deps import APIError, AppState, Principal, require, session, state
from aegis.clients.controlplane import ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.db.models import ServiceStatus
from aegis.engine.topology import TopologyBuilder
from aegis.security.auth import Permission

router = APIRouter(prefix="/api/v1", tags=["services"])
READ = Depends(require(Permission.READ))

# Only these parameterized queries can be executed through the API (no arbitrary PromQL).
METRICS = {
    "rps": 'sum(rate(shop_http_requests_total{{namespace="{ns}",service="{s}"}}[30s]))',
    # "or vector(0)": with no 5xx samples the numerator is empty; plot 0 instead of a gap.
    "error_ratio": '(sum(rate(shop_http_requests_total{{namespace="{ns}",service="{s}",code=~"5.."}}[30s])) or vector(0)) / '
                   'sum(rate(shop_http_requests_total{{namespace="{ns}",service="{s}"}}[30s]))',
    "p95_ms": 'histogram_quantile(0.95, sum by (le) (rate(shop_http_request_duration_seconds_bucket'
              '{{namespace="{ns}",service="{s}"}}[30s]))) * 1000',
    "cpu_cores": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container="{c}"}}[30s]))',
    "memory_mb": 'sum(container_memory_working_set_bytes{{namespace="{ns}",container="{c}"}}) / 1048576',
    "db_pool_in_use": 'sum(shop_db_pool_in_use{{namespace="{ns}",service="{s}"}})',
    "customer_success_ratio": 'sum(rate(loadgen_requests_total{{outcome="success"}}[30s])) / sum(rate(loadgen_requests_total[30s]))',
}


def _topology(st: AppState) -> TopologyBuilder:
    return TopologyBuilder(st.cp, st.prom, st.jaeger, st.settings.managed_namespace)


async def _workloads(st: AppState) -> dict[str, dict[str, Any]]:
    try:
        return {w["name"]: w for w in await st.cp.workloads(st.settings.managed_namespace)}
    except (ControlPlaneError, UpstreamUnavailable):
        return {}


def _version(w: dict[str, Any]) -> str | None:
    """Release version of the current rollout revision (pod template label), if recorded."""
    current = next((r for r in w.get("revisions", []) if r.get("revision") == w.get("revision")), {})
    return current.get("version") or (w.get("labels") or {}).get("version")


@router.get("/services")
async def services(_: Principal = READ, st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    rows = {r.service: r for r in (await s.execute(select(ServiceStatus))).scalars()}
    workloads = await _workloads(st)
    items = []
    for name in sorted(set(rows) | set(workloads)):
        r, w = rows.get(name), workloads.get(name, {})
        items.append({"name": name, "status": r.status if r else "unknown", "signals": r.signals if r else {},
                      "updated_at": r.updated_at.isoformat() if r else None, "tier": w.get("tier"),
                      "replicas": w.get("replicas"), "ready": w.get("readyReplicas"), "revision": w.get("revision"),
                      "version": _version(w), "owner": w.get("owner"),
                      "dependencies": w.get("dependencies", []), "dependents": w.get("dependents", []),
                      "rollout_complete": w.get("rolloutComplete")})
    return {"items": items, "controlplane_available": bool(workloads)}


@router.get("/services/{name}")
async def service_detail(name: str, _: Principal = READ, st: AppState = Depends(state),
                         s: AsyncSession = Depends(session)) -> dict[str, Any]:
    row = await s.get(ServiceStatus, name)
    try:
        w = await st.cp.workload(st.settings.managed_namespace, name)
        changes = await st.cp.changes(datetime.now(UTC) - timedelta(hours=6), st.settings.managed_namespace, name)
    except ControlPlaneError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc
    except UpstreamUnavailable as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc
    return {"name": name, "status": row.status if row else "unknown", "signals": row.signals if row else {},
            "workload": w, "changes": list(reversed(changes))[:30]}


@router.get("/services/{name}/metrics")
async def service_metrics(name: str, minutes: int = Query(30, ge=5, le=360), _: Principal = READ,
                          st: AppState = Depends(state)) -> dict[str, Any]:
    if not name.replace("-", "").isalnum() or len(name) > 63:
        raise APIError(400, "bad_request", "invalid service name")
    workloads = await _workloads(st)
    container = next((c["name"] for c in workloads.get(name, {}).get("containers", [])), name)
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    step = f"{max(5, minutes * 60 // 180)}s"
    out: dict[str, Any] = {}
    for key, tmpl in METRICS.items():
        q = tmpl.format(ns=st.settings.managed_namespace, s=name, c=container)
        try:
            series = await st.prom.range(q, start, end, step)
        except UpstreamUnavailable as exc:
            raise APIError(502, "prometheus_unavailable", str(exc)) from exc
        out[key] = [[int(t), round(v, 5)] for t, v in (series[0]["points"] if series else [])]
    return {"service": name, "start": start.isoformat(), "end": end.isoformat(), "step": step, "series": out}


@router.get("/changes")
async def changes(hours: int = Query(2, ge=1, le=24), _: Principal = READ, st: AppState = Depends(state)) -> dict[str, Any]:
    try:
        items = await st.cp.changes(datetime.now(UTC) - timedelta(hours=hours), st.settings.managed_namespace)
    except (ControlPlaneError, UpstreamUnavailable) as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc
    return {"items": list(reversed(items))}


@router.get("/topology")
async def topology(_: Principal = READ, st: AppState = Depends(state), s: AsyncSession = Depends(session)) -> dict[str, Any]:
    try:
        g = await _topology(st).build()
    except (ControlPlaneError, UpstreamUnavailable) as exc:
        raise APIError(502, "controlplane_unavailable", str(exc)) from exc
    status = {r.service: r.status for r in (await s.execute(select(ServiceStatus))).scalars()}
    doc = g.as_dict()
    for n in doc["nodes"]:
        n["status"] = status.get(n["name"], "unknown")
    anomalous = {k for k, v in status.items() if v in ("degraded", "down")}
    doc["rootCandidates"] = g.root_candidates(anomalous)
    return doc


@router.get("/topology/impact/{service}")
async def impact(service: str, _: Principal = READ, st: AppState = Depends(state)) -> dict[str, Any]:
    """If `service` is unhealthy, which upstream services are likely to degrade?"""
    g = await _topology(st).build()
    if service not in g.nodes:
        raise APIError(404, "not_found", f"{service} is not in the dependency graph")
    return {"service": service, "blast_radius": [{"service": u, "path": list(reversed(g.path(u, service)))}
                                                  for u in g.upstream(service)]}


@router.get("/topology/explain/{service}")
async def explain(service: str, _: Principal = READ, st: AppState = Depends(state),
                  s: AsyncSession = Depends(session)) -> dict[str, Any]:
    """Which dependencies could explain a symptom observed on `service`?"""
    g = await _topology(st).build()
    if service not in g.nodes:
        raise APIError(404, "not_found", f"{service} is not in the dependency graph")
    status = {r.service: r.status for r in (await s.execute(select(ServiceStatus))).scalars()}
    anomalous = {k for k, v in status.items() if v in ("degraded", "down")}
    return {"service": service, "candidates": g.explain(service, anomalous)}
