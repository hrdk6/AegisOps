"""aegis-engine: detection loop, incident lifecycle and background services."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text

from aegis.ai.router import CallRecord, ModelRouter
from aegis.clients.controlplane import ControlPlane, ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.clients.jaeger import Jaeger
from aegis.clients.loki import Loki
from aegis.clients.prometheus import Prometheus
from aegis.config import Settings, get_settings
from aegis.db import audit, repo
from aegis.db.listen import Listener
from aegis.db.models import Command, ComponentHeartbeat, Incident, ModelCall
from aegis.db.session import Database
from aegis.domain.enums import IncidentStatus, Severity
from aegis.domain.schemas import ServiceSignals
from aegis.engine.context import ContextEngine
from aegis.engine.detector import Detector
from aegis.engine.diagnosis.engine import DiagnosisEngine
from aegis.engine.orchestrator import EngineDeps, Orchestrator
from aegis.engine.planner import Planner
from aegis.engine.postmortem import PostmortemGenerator
from aegis.engine.slo import SLOBook
from aegis.engine.topology import TopologyBuilder
from aegis.engine.verification import Verifier
from aegis.knowledge import runbooks as kb
from aegis.logs import fields, setup_logging
from aegis.telemetry import setup_tracing

log = logging.getLogger("aegis.engine")

CYCLE = Histogram("aegis_engine_detection_cycle_seconds", "Detection cycle duration")
INCIDENTS = Counter("aegis_incidents_created_total", "Incidents created", ["severity"])
SERVICE_STATUS = Gauge("aegis_service_status", "1 when the service is in the given status", ["service", "status"])
ANOMALIES = Gauge("aegis_active_anomalies", "Active confirmed anomalies", ["service", "signal"])

SIGNAL_PRIORITY = ["scaled_to_zero", "crashloop", "error_ratio", "oom_killed", "latency_p95", "db_pool_saturation",
                   "unavailable_replicas", "cpu_saturation", "memory_pressure", "cpu_throttling", "traffic_drop"]
SIGNAL_LABEL = {"scaled_to_zero": "Service scaled to zero", "crashloop": "Pods crash-looping",
                "error_ratio": "Elevated 5xx error rate", "oom_killed": "Containers OOMKilled",
                "latency_p95": "Latency SLO breach", "db_pool_saturation": "DB connection pool saturated",
                "unavailable_replicas": "Replicas unavailable", "cpu_saturation": "CPU saturation",
                "memory_pressure": "Memory pressure", "cpu_throttling": "CPU throttling", "traffic_drop": "Traffic drop"}
SEVERITY_RANK = {"SEV3": 1, "SEV2": 2, "SEV1": 3}


def incident_title(degraded: dict[str, ServiceSignals], entry: list[str]) -> str:
    def key(item: tuple[str, ServiceSignals]) -> tuple[int, int]:
        svc, sig = item
        best = min(SIGNAL_PRIORITY.index(a.signal) if a.signal in SIGNAL_PRIORITY else 99 for a in sig.anomalies)
        return best, 0 if svc in entry else 1

    svc, sig = sorted(degraded.items(), key=key)[0]
    signal = min((a.signal for a in sig.anomalies), key=lambda s: SIGNAL_PRIORITY.index(s) if s in SIGNAL_PRIORITY else 99)
    more = f" (+{len(degraded) - 1} more)" if len(degraded) > 1 else ""
    return f"{SIGNAL_LABEL.get(signal, signal)} on {svc}{more}"


def severity(degraded: dict[str, ServiceSignals], entry: list[str], tiers: dict[str, str]) -> Severity:
    if any(s in entry and ((sig.error_ratio or 0) >= 0.10 or sig.status == "down") for s, sig in degraded.items()):
        return Severity.SEV1
    critical = [s for s in degraded if tiers.get(s) == "critical"]
    if len(critical) >= 3:
        return Severity.SEV1
    if critical or any(tiers.get(s) == "stateful" for s in degraded):
        return Severity.SEV2
    return Severity.SEV3


def symptoms_of(degraded: dict[str, ServiceSignals]) -> list[dict[str, Any]]:
    return [{"service": s, "signal": a.signal, "value": a.value, "reason": a.reason, "since": a.since.isoformat()}
            for s, sig in degraded.items() for a in sig.anomalies]


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.db = Database(settings.dsn)
        self.cp = ControlPlane(settings.controlplane_url, settings.controlplane_bearer)
        self.prom = Prometheus(settings.prometheus_url)
        self.loki = Loki(settings.loki_url)
        self.jaeger = Jaeger(settings.jaeger_url)
        self.slos = SLOBook.load(settings.slo_file)
        self.router = ModelRouter.from_settings(settings.llm_routes, settings.llm_prices, settings.llm_timeout_seconds,
                                                settings.llm_max_retries, settings.llm_max_calls_per_incident,
                                                recorder=self._record_model_call)
        self.runbooks = kb.load_dir(settings.runbook_dir)
        self.detector = Detector(self.prom, self.cp, self.slos, settings.managed_namespace, settings.detection_persistence)
        topology = TopologyBuilder(self.cp, self.prom, self.jaeger, settings.managed_namespace)
        self.topology = topology
        planner = Planner(self.cp, settings.managed_namespace, router=self.router)
        self.orch = Orchestrator(EngineDeps(
            settings=settings, db=self.db, cp=self.cp, detector=self.detector,
            context=ContextEngine(self.cp, self.prom, self.loki, self.jaeger, topology, self.slos,
                                  settings.managed_namespace, jaeger_url=settings.jaeger_public_url),
            diagnosis=DiagnosisEngine(self.router), planner=planner,
            verifier=Verifier(self.detector, self.slos, settings.verification_timeout_seconds,
                              settings.verification_min_settle_seconds),
            postmortem=PostmortemGenerator(self.db, self.prom, self.router, self.runbooks), runbooks=self.runbooks))
        planner.history = self.orch.history_efficacy
        self.last_cycle: float = 0.0
        self.tiers: dict[str, str] = {}
        self.audit_cursor: dict[str, Any] = {"boot": None, "seq": 0}
        self.commands = Listener(settings.raw_dsn, "aegis_commands", self._on_command)

    async def _record_model_call(self, incident_id: str | None, rec: CallRecord) -> None:
        async with self.db.session() as s:
            s.add(ModelCall(incident_id=incident_id, purpose=rec.purpose, provider=rec.provider, model=rec.model,
                            status=rec.status, latency_ms=rec.latency_ms, prompt_tokens=rec.prompt_tokens,
                            completion_tokens=rec.completion_tokens, cost_usd=rec.cost_usd or 0.0, attempt=rec.attempt,
                            fallback=rec.fallback, error=rec.error))

    # ------------------------------------------------------------- startup --
    async def start(self) -> None:
        for attempt in range(60):
            try:
                async with self.db.session() as s:
                    await s.execute(text("SELECT 1 FROM incidents LIMIT 1"))
                    await kb.sync(s, self.runbooks)
                    hb = await s.get(ComponentHeartbeat, "audit-sync")
                    if hb and hb.details:
                        self.audit_cursor = dict(hb.details)
                break
            except Exception as exc:
                log.warning("waiting for database schema", extra=fields(attempt=attempt, error=str(exc)[:200]))
                await asyncio.sleep(2)
        log.info("engine started", extra=fields(runbooks=len(self.runbooks), llm_routes=self.router.status(),
                                                namespace=self.s.managed_namespace))
        self.commands.start()
        await self._resume()

    async def _resume(self) -> None:
        async with self.db.session() as s:
            # Approvals left pending by a previous engine process: closed incidents' ones are moot, and resumed
            # workflows restart from investigation (a fresh approval is requested if still needed).
            orphaned = await repo.close_pending_approvals(s, "expired")
            for inc in await repo.open_incidents(s):
                orphaned += await repo.close_pending_approvals(s, "superseded", inc.id)
                await repo.add_event(s, inc.id, "workflow_resumed", "Engine (re)started; resuming the incident workflow")
                self.orch.ensure(inc.id)
        if orphaned:
            log.info("closed orphaned approvals", extra=fields(approvals=orphaned))

    # ------------------------------------------------------------ detection --
    async def detection_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                with CYCLE.time():
                    signals = await self.detector.evaluate()
                    self.tiers = {w["name"]: w.get("tier", "standard") for w in self.detector.workloads}
                    await self._persist_status(signals)
                    await self._handle(signals)
                self.last_cycle = time.time()
            except Exception:
                log.exception("detection cycle failed")
            await asyncio.sleep(max(0.5, self.s.detect_interval_seconds - (time.monotonic() - t0)))

    async def _persist_status(self, signals: dict[str, ServiceSignals]) -> None:
        ANOMALIES.clear()
        async with self.db.session() as s:
            for name, sig in signals.items():
                await repo.upsert_service_status(s, name, sig.status, sig.model_dump(mode="json"))
                for st in ("healthy", "degraded", "down", "unknown"):
                    SERVICE_STATUS.labels(name, st).set(1 if sig.status == st else 0)
                for a in sig.anomalies:
                    ANOMALIES.labels(name, a.signal).set(1)

    async def _handle(self, signals: dict[str, ServiceSignals]) -> None:
        degraded = {k: v for k, v in signals.items() if v.anomalies}
        if not degraded:
            return
        entry = self.slos.entry_services
        now = datetime.now(UTC)
        async with self.db.session() as s:
            open_ = await repo.open_incidents(s)
            target: Incident | None = None
            for inc in open_:
                if inc.detected_at >= now - timedelta(seconds=self.s.incident_merge_window_seconds):
                    target = inc
                    break
            if target is None:
                for iid in list(self.orch.watching):
                    target = await s.get(Incident, iid)
                    if target:
                        break
            if target is None:
                # An escalated incident is owned by humans: while its symptoms persist, further anomalies on
                # the same services belong to it instead of opening a fresh incident every merge window.
                held = await repo.escalated_holding(s, sorted(degraded),
                                                    now - timedelta(seconds=self.s.escalation_hold_seconds))
                if held is not None:
                    known = {(x.get("service"), x.get("signal")) for x in held.symptoms if isinstance(x, dict)}
                    new_symptoms = [x for x in symptoms_of(degraded) if (x["service"], x["signal"]) not in known]
                    if new_symptoms:
                        held.symptoms = list(held.symptoms) + new_symptoms
                        held.affected_services = sorted(set(held.affected_services) | set(degraded))
                        await repo.add_event(s, held.id, "symptom_added", "Additional symptoms while escalated: " +
                                             "; ".join(f"{x['service']} {x['signal']}" for x in new_symptoms[:6]),
                                             data={"symptoms": new_symptoms})
                    held.updated_at = now
                    return
            if target is not None:
                new_services = sorted(set(degraded) - set(target.affected_services))
                known = {(x.get("service"), x.get("signal")) for x in target.symptoms if isinstance(x, dict)}
                new_symptoms = [x for x in symptoms_of(degraded) if (x["service"], x["signal"]) not in known]
                if new_services or new_symptoms:
                    target.affected_services = sorted(set(target.affected_services) | set(degraded))
                    target.symptoms = list(target.symptoms) + new_symptoms
                    sev = severity(degraded, entry, self.tiers)
                    if SEVERITY_RANK[sev.value] > SEVERITY_RANK[target.severity]:
                        target.severity = sev.value
                    await repo.add_event(s, target.id, "symptom_added", "Additional symptoms: " + "; ".join(
                        f"{x['service']} {x['signal']}" for x in new_symptoms[:6]), data={"symptoms": new_symptoms})
                if not IncidentStatus(target.status).terminal and not self.orch.active(target.id):
                    self.orch.ensure(target.id)
                return
            onset = min(a.since for sig in degraded.values() for a in sig.anomalies)
            sev = severity(degraded, entry, self.tiers)
            inc = Incident(id=repo.new_incident_id(now), title=incident_title(degraded, entry), severity=sev.value,
                           status=IncidentStatus.DETECTED.value, detected_at=now, onset_at=onset,
                           affected_services=sorted(degraded), symptoms=symptoms_of(degraded), summary="")
            s.add(inc)
            await s.flush()
            for x in symptoms_of(degraded):
                await repo.add_event(s, inc.id, "anomaly_detected", f"{x['service']}: {x['reason']}",
                                     actor="aegis-detector", data=x)
            await repo.add_event(s, inc.id, "incident_created", f"Incident created: {inc.title} ({sev.value})",
                                 data={"severity": sev.value, "affected": sorted(degraded)})
            await audit.append(s, actor="aegis-engine", action="incident.create", resource=f"incident/{inc.id}",
                               outcome="created", details={"severity": sev.value, "services": sorted(degraded)})
            iid = inc.id
        INCIDENTS.labels(sev.value).inc()
        log.info("incident created", extra=fields(incident_id=iid, severity=sev.value, services=sorted(degraded)))
        self.orch.ensure(iid)

    # ------------------------------------------------------------- commands --
    async def _on_command(self, payload: str) -> None:
        async with self.db.session() as s:
            cmd = await s.get(Command, int(payload))
            if cmd is None or cmd.processed_at is not None:
                return
            cmd.processed_at = datetime.now(UTC)
            result = "ok"
            if cmd.type == "investigate" and cmd.incident_id:
                inc = await s.get(Incident, cmd.incident_id)
                if inc and IncidentStatus(inc.status).terminal:
                    inc.outcome = None
                    inc.resolved_at = None
                    await repo.set_status(s, inc, IncidentStatus.INVESTIGATING,
                                          f"Re-investigation requested by {cmd.created_by}", actor=cmd.created_by)
                result = "started" if cmd.incident_id else "missing incident"
            elif cmd.type not in ("resolve", "escalate"):
                result = "unknown command"
            cmd.result = result
        if cmd.type == "investigate" and cmd.incident_id:
            self.orch.ensure(cmd.incident_id)
        elif cmd.type == "resolve" and cmd.incident_id:
            await self.orch.resolve_manually(cmd.incident_id, cmd.created_by, str(cmd.payload.get("note", "")))
        elif cmd.type == "escalate" and cmd.incident_id:
            task = self.orch.tasks.get(cmd.incident_id)
            if task and not task.done():
                task.cancel()
            await self.orch._escalate(cmd.incident_id, f"escalated manually by {cmd.created_by}")
            await self.orch._finish(cmd.incident_id)

    # -------------------------------------------------- background services --
    async def audit_sync_loop(self) -> None:
        """Ingest the controller's audit ring into the hash-chained audit log."""
        while True:
            try:
                res = await self.cp.audit(int(self.audit_cursor.get("seq") or 0), self.audit_cursor.get("boot"))
                if res.get("boot") != self.audit_cursor.get("boot"):
                    self.audit_cursor = {"boot": res.get("boot"), "seq": 0}
                items = res.get("items", [])
                if items:
                    async with self.db.session() as s:
                        for ev in items:
                            await audit.append(s, actor=ev.get("actor", "controller"), action=ev.get("action", ""),
                                               resource=ev.get("resource", ""), outcome=ev.get("outcome", ""),
                                               details=ev.get("details") or {}, source="controller",
                                               ts=datetime.fromisoformat(ev["time"].replace("Z", "+00:00")))
                            self.audit_cursor["seq"] = ev["seq"]
                        await repo.heartbeat(s, "audit-sync", "ok", dict(self.audit_cursor))
            except (UpstreamUnavailable, ControlPlaneError) as exc:
                log.warning("controller audit sync failed", extra=fields(error=str(exc)[:200]))
            except Exception:
                log.exception("audit sync failed")
            await asyncio.sleep(5)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                async with self.db.session() as s:
                    open_count = len(await repo.open_incidents(s))
                    await repo.heartbeat(s, "engine", "ok" if time.time() - self.last_cycle < 30 else "degraded", {
                        "last_cycle": datetime.fromtimestamp(self.last_cycle, UTC).isoformat() if self.last_cycle else None,
                        "degraded_sources": sorted(self.detector.degraded_sources), "open_incidents": open_count,
                        "active_workflows": sum(1 for t in self.orch.tasks.values() if not t.done()),
                        "llm_routes": self.router.status(), "config_fingerprint": self.s.fingerprint()})
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(10)

    def http_app(self) -> FastAPI:
        app = FastAPI(title="aegis-engine", docs_url=None, redoc_url=None)

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/readyz")
        async def readyz() -> JSONResponse:
            ok = time.time() - self.last_cycle < 30
            return JSONResponse({"ready": ok, "degraded_sources": sorted(self.detector.degraded_sources)},
                                status_code=200 if ok else 503)

        @app.get("/metrics")
        async def metrics() -> PlainTextResponse:
            return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        @app.get("/debug/state")
        async def state() -> dict[str, Any]:
            return {"latest": {k: v.model_dump(mode="json") for k, v in self.detector.latest.items()},
                    "workflows": {k: not t.done() for k, t in self.orch.tasks.items()},
                    "watching": sorted(self.orch.watching)}

        return app


async def main() -> None:
    settings = get_settings()
    setup_logging("aegis-engine", settings.log_level)
    setup_tracing("aegis-engine", settings.otel_exporter_otlp_endpoint)
    engine = Engine(settings)
    await engine.start()
    server = uvicorn.Server(uvicorn.Config(engine.http_app(), host="0.0.0.0", port=8090, log_level="warning",  # noqa: S104
                                           access_log=False))
    await asyncio.gather(engine.detection_loop(), engine.audit_sync_loop(), engine.heartbeat_loop(), server.serve())


def run() -> None:
    asyncio.run(main())

