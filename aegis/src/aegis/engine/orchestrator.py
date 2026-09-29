"""Incident orchestrator: the bounded closed loop.

    INVESTIGATING -> DIAGNOSED -> [simulate] -> AWAITING_APPROVAL? -> EXECUTING -> VERIFYING
        -> RESOLVED (+ recurrence watch) | ROLLED_BACK -> next round | ESCALATED

Loop safety on this side (the controller enforces its own, authoritative
budgets): at most `max_remediation_rounds` rounds, executed actions are never
re-proposed, every wait has a timeout, one workflow per incident, bounded
concurrency across incidents, and any internal failure escalates to a human.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from sqlalchemy import func, select

from aegis.clients.controlplane import ControlPlane, ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.config import Settings
from aegis.db import repo
from aegis.db.models import ActionRecord, ApprovalRecord, Incident, SimulationRecord, VerificationRecord
from aegis.db.session import Database
from aegis.domain.enums import CauseCategory, EvidenceKind, IncidentStatus, VerificationOutcome
from aegis.domain.schemas import Diagnosis, Evidence, EvidenceSource, RemediationCandidate, RemediationPlan
from aegis.engine.context import ContextBundle, ContextEngine
from aegis.engine.detector import Detector
from aegis.engine.diagnosis.engine import DiagnosisEngine
from aegis.engine.planner import Planner
from aegis.engine.postmortem import PostmortemGenerator
from aegis.engine.verification import Verifier
from aegis.knowledge import runbooks as kb
from aegis.logs import incident_id_var

log = logging.getLogger("aegis.orchestrator")
tracer = trace.get_tracer("aegis.engine")

TERMINAL_ACTION = {"Succeeded", "Failed", "Denied", "Rejected", "Expired", "RolledBack"}
# Low-confidence diagnoses are re-run a bounded number of times as symptoms develop.
INCONCLUSIVE_RETRIES = 2


class Escalate(Exception):
    """Stop the autonomous loop and hand over to a human."""


@dataclass
class EngineDeps:
    settings: Settings
    db: Database
    cp: ControlPlane
    detector: Detector
    context: ContextEngine
    diagnosis: DiagnosisEngine
    planner: Planner
    verifier: Verifier
    postmortem: PostmortemGenerator
    runbooks: list[kb.Runbook]


class Orchestrator:
    def __init__(self, deps: EngineDeps) -> None:
        self.d = deps
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.watching: set[str] = set()
        self.sem = asyncio.Semaphore(3)

    # --------------------------------------------------------------- entry --
    def ensure(self, incident_id: str) -> bool:
        task = self.tasks.get(incident_id)
        if task and not task.done():
            return False
        self.tasks[incident_id] = asyncio.create_task(self.run(incident_id), name=f"incident-{incident_id}")
        return True

    def active(self, incident_id: str) -> bool:
        t = self.tasks.get(incident_id)
        return bool(t and not t.done()) or incident_id in self.watching

    async def run(self, incident_id: str) -> None:
        async with self.sem:
            incident_id_var.set(incident_id)
            with tracer.start_as_current_span("incident.workflow", attributes={"aegis.incident_id": incident_id}):
                try:
                    await self._workflow(incident_id)
                except Escalate as exc:
                    await self._escalate(incident_id, str(exc))
                except Exception as exc:  # fail safe: never leave an incident in an autonomous state
                    log.exception("workflow failed")
                    await self._escalate(incident_id, f"internal workflow error ({type(exc).__name__}); human review required")
                finally:
                    await self._finish(incident_id)

    # ------------------------------------------------------------ workflow --
    async def _workflow(self, iid: str) -> None:
        s = self.d.settings
        executed: set[str] = set()
        for round_no in range(1, s.max_remediation_rounds + 1):
            async with self.d.db.session() as ses:
                inc = await ses.get(Incident, iid)
                if inc is None or IncidentStatus(inc.status).terminal:
                    return
                inc.iteration = round_no
                await repo.set_status(ses, inc, IncidentStatus.INVESTIGATING,
                                      f"Investigation round {round_no} started", {"round": round_no})
                affected, onset, title = list(inc.affected_services), inc.onset_at, inc.title
            for attempt in range(1, INCONCLUSIVE_RETRIES + 2):
                bundle, dx = await self._investigate(iid, title, affected, onset, round_no)
                if dx.selected is not None or attempt > INCONCLUSIVE_RETRIES or self._recovered(affected):
                    break
                async with self.d.db.session() as ses:
                    await repo.add_event(ses, iid, "investigation_extended",
                                         f"Diagnosis inconclusive ({_leading(dx)}); collecting more evidence "
                                         f"before deciding (attempt {attempt}/{INCONCLUSIVE_RETRIES})")
                for _ in range(3):
                    await self.d.detector.next_cycle(timeout=15)
                affected = await self._affected(iid)
            plan = await self._plan(iid, dx, bundle, executed)
            if plan.selected_index is None:
                await self._no_action(iid, plan)
                return
            if self._recovered(affected):
                await self._resolve(iid, "self_recovered", "Symptoms cleared before any action was taken")
                return
            outcome = await self._act(iid, dx, plan, bundle, executed)
            if outcome == "resolved":
                relapsed = await self._recurrence_watch(iid, affected)
                if not relapsed:
                    return
                async with self.d.db.session() as ses:
                    inc = await ses.get(Incident, iid)
                    inc.resolved_at = None
                    inc.outcome = None
                    await repo.add_event(ses, iid, "relapse_detected",
                                         "Symptoms returned during the recurrence watch; reopening the investigation")
        raise Escalate(f"remediation budget exhausted after {s.max_remediation_rounds} rounds without sustained recovery")

    async def _investigate(self, iid: str, title: str, affected: list[str], onset: datetime | None,
                           round_no: int) -> tuple[ContextBundle, Diagnosis]:
        with tracer.start_as_current_span("incident.investigate"):
            history = await self._history(affected)
            bundle = await self.d.context.collect(iid, dict(self.d.detector.latest), affected, onset, history)
        async with self.d.db.session() as ses:
            await repo.save_evidence(ses, iid, round_no, bundle.evidence)
            kinds: dict[str, int] = {}
            for e in bundle.evidence:
                kinds[e.kind.value] = kinds.get(e.kind.value, 0) + 1
            await repo.add_event(ses, iid, "evidence_collected",
                                 f"Collected {len(bundle.evidence)} evidence items "
                                 f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))})"
                                 + (f"; gaps: {'; '.join(bundle.gaps)}" if bundle.gaps else ""),
                                 data={"counts": kinds, "gaps": bundle.gaps})
            await repo.snapshot(ses, iid, f"context-round{round_no}", bundle.to_dict())
        with tracer.start_as_current_span("incident.diagnose"):
            dx = await self.d.diagnosis.diagnose(iid, title, bundle, round_no)
        if dx.selected is not None:
            await self._attach_runbooks(iid, bundle, dx, round_no)
        async with self.d.db.session() as ses:
            await repo.save_diagnosis(ses, dx)
            inc = await ses.get(Incident, iid)
            if dx.selected:
                inc.root_service, inc.category, inc.confidence = dx.selected.component, dx.selected.category.value, dx.confidence
            inc.summary = dx.summary
            await repo.set_status(ses, inc, IncidentStatus.DIAGNOSED, dx.summary,
                                  {"confidence": dx.confidence, "method": dx.method,
                                   "hypotheses": [f"{h.category.value}:{h.component}:{h.confidence}" for h in dx.hypotheses[:4]]})
            if dx.selected:
                await repo.add_event(ses, iid, "root_cause_identified",
                                     f"{dx.selected.component} identified as probable root ({dx.selected.category.value}, "
                                     f"{dx.confidence:.0%}); blast radius: {', '.join(dx.blast_radius) or 'none'}")
        return bundle, dx

    async def _attach_runbooks(self, iid: str, bundle: ContextBundle, dx: Diagnosis, round_no: int) -> None:
        assert dx.selected is not None
        try:
            async with self.d.db.session() as ses:
                hits = await kb.search(ses, dx.selected.category.value, dx.selected.statement, limit=2)
        except Exception:
            hits = kb.search_local(self.d.runbooks, dx.selected.category.value, dx.selected.statement, limit=2)
        items = []
        for h in hits:
            ev = Evidence(id=Evidence.make_id("runbook", None, h["id"]), kind=EvidenceKind.RUNBOOK, signal="runbook",
                          title=f"Runbook {h['id']}: {h['title']} (section: {h['section']})",
                          summary=h["snippet"][:600], data=h,
                          source=EvidenceSource(system="knowledge", query=f"category={dx.selected.category.value}", url=h["path"]),
                          observed_at=datetime.now(UTC), score=0.4)
            bundle.add(f"runbook:{h['id']}", ev)
            items.append(ev)
        if items:
            async with self.d.db.session() as ses:
                await repo.save_evidence(ses, iid, round_no, items)

    async def _plan(self, iid: str, dx: Diagnosis, bundle: ContextBundle, executed: set[str]) -> RemediationPlan:
        with tracer.start_as_current_span("incident.plan"):
            plan = await self.d.planner.plan(dx, bundle, exclude=executed)
        async with self.d.db.session() as ses:
            await repo.save_plan(ses, plan)
            sel = plan.candidates[plan.selected_index] if plan.selected_index is not None else None
            await repo.add_event(ses, iid, "remediation_proposed",
                                 f"{len(plan.candidates)} remediation candidate(s) generated"
                                 + (f"; selected {sel.action_type.value} on {sel.target.name} "
                                    f"(risk {sel.risk_level}, utility {sel.utility})" if sel else
                                    f"; no action selected: {plan.escalation_reason}"),
                                 data={"plan": plan.id, "candidates": [
                                     {"action": c.action_type.value, "target": c.target.name, "risk": c.risk_level,
                                      "allowed": c.policy_allowed, "approval": c.requires_approval, "utility": c.utility}
                                     for c in plan.candidates]})
        return plan

    async def _no_action(self, iid: str, plan: RemediationPlan) -> None:
        reason = plan.escalation_reason or "no viable remediation"
        affected = await self._affected(iid)
        if reason.startswith("monitor"):
            if await self._await_recovery(affected, 150):
                await self._resolve(iid, "auto_mitigated", "Recovered after automatic mitigation (" + reason + ")")
                return
            raise Escalate("recovery did not follow automatic mitigation: " + reason)
        if await self._await_recovery(affected, 10):
            await self._resolve(iid, "self_recovered", "Symptoms cleared without intervention")
            return
        raise Escalate(reason)

    # ------------------------------------------------------------- actions --
    async def _act(self, iid: str, dx: Diagnosis, plan: RemediationPlan, bundle: ContextBundle, executed: set[str]) -> str:
        viable = [c for c in plan.candidates if c.policy_allowed and c.utility > 0]
        for cand in viable:
            if cand.fingerprint() in executed:
                continue
            sim_ref = None
            if cand.simulatable and cand.risk_level in ("MEDIUM", "HIGH"):
                verdict, sim_ref = await self._simulate(iid, cand)
                if verdict == "Regressed":
                    executed.add(cand.fingerprint())
                    continue
                if verdict != "Improved":
                    sim_ref = None
            action = await self._submit(iid, plan, cand, dx, sim_ref)
            if action is None:
                executed.add(cand.fingerprint())
                continue
            phase = action["phase"]
            if phase == "AwaitingApproval":
                action = await self._await_approval(iid, action, cand, dx, sim_ref)
                phase = action["phase"]
                if phase in ("Rejected", "Expired"):
                    raise Escalate(f"action {action['name']} was {phase.lower()} by a human; autonomous remediation stopped")
                if phase == "Recovered":
                    await self._resolve(iid, "self_recovered", "Symptoms cleared while awaiting approval")
                    return "resolved"
            if phase == "Denied":
                executed.add(cand.fingerprint())
                continue
            executed.add(cand.fingerprint())
            return await self._execute_and_verify(iid, action, cand, dx)
        return "no_action"

    async def _submit(self, iid: str, plan: RemediationPlan, cand: RemediationCandidate, dx: Diagnosis,
                      sim_ref: str | None) -> dict[str, Any] | None:
        req = {"incidentId": iid, "actionType": cand.action_type.value, "target": cand.target.model_dump(),
               "parameters": cand.params.wire(), "justification": cand.rationale[:1900],
               "diagnosisConfidence": int(round(dx.confidence * 100))}
        if sim_ref:
            req["simulationRef"] = sim_ref
        if cand.expected_revision:
            req["preconditions"] = {"expectedRevision": cand.expected_revision}
        try:
            action = await self.d.cp.submit_action(req)
        except (ControlPlaneError, UpstreamUnavailable) as exc:
            async with self.d.db.session() as ses:
                await repo.add_event(ses, iid, "action_rejected", f"Control plane rejected {cand.action_type.value}: {exc}")
            return None
        await self._sync_action(iid, action, plan=plan, cand=cand)
        dec = action.get("decision") or {}
        async with self.d.db.session() as ses:
            await repo.add_event(ses, iid, "policy_decision",
                                 f"Policy {'allowed' if dec.get('allowed') else 'denied'} {cand.action_type.value} on "
                                 f"{cand.target.name}: risk {dec.get('riskLevel')} ({dec.get('riskScore')}), "
                                 f"{'approval required' if dec.get('requiresApproval') else 'no approval required'}"
                                 + (f"; reasons: {'; '.join(dec.get('reasons', [])[:3])}" if dec.get("reasons") else ""),
                                 actor="aegis-controller", data={"action": action["name"], "decision": dec})
        return action

    async def _await_action(self, iid: str, action: dict[str, Any], targets: set[str], timeout: float,
                            watch_recovery: list[str] | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while action["phase"] not in targets and time.monotonic() < deadline:
            remaining = int(max(1, min(25, deadline - time.monotonic())))
            try:
                nxt = await self.d.cp.action(action["name"], wait_for_version=action["resourceVersion"], timeout=remaining)
            except (UpstreamUnavailable, ControlPlaneError) as exc:
                log.warning("action poll failed", extra={"fields": {"error": str(exc)}})
                await asyncio.sleep(2)
                continue
            if nxt["resourceVersion"] != action["resourceVersion"]:
                await self._sync_action(iid, nxt)
            action = nxt
            if watch_recovery is not None and self._recovered(watch_recovery):
                return {**action, "phase": "Recovered"}
        return action

    async def _await_approval(self, iid: str, action: dict[str, Any], cand: RemediationCandidate, dx: Diagnosis,
                              sim_ref: str | None) -> dict[str, Any]:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            ses.add(ApprovalRecord(id=action["name"], incident_id=iid, status="pending", spec_hash=action.get("specHash", ""),
                                   context={"candidate": cand.model_dump(mode="json"), "diagnosis": dx.summary,
                                            "confidence": dx.confidence, "evidence": (dx.selected.supporting_evidence
                                                                                      if dx.selected else []),
                                            "simulation": sim_ref, "decision": action.get("decision")}))
            await repo.set_status(ses, inc, IncidentStatus.AWAITING_APPROVAL,
                                  f"Human approval required for {cand.action_type.value} on {cand.target.name}",
                                  {"action": action["name"], "reasons": (action.get("decision") or {}).get("reasons", [])})
            await repo.add_event(ses, iid, "approval_requested",
                                 f"Approval requested for {action['name']} ({cand.action_type.value} on {cand.target.name})")
        result = await self._await_action(iid, action, {"Approved", "Executing", "Succeeded", "Failed", "Rejected", "Expired",
                                                        "Denied"}, self.d.settings.approval_wait_seconds,
                                          watch_recovery=await self._affected(iid))
        async with self.d.db.session() as ses:
            ap = await ses.get(ApprovalRecord, action["name"])
            appr = result.get("approval") or {}
            if result["phase"] == "Recovered":
                ap.status = "superseded"
            elif appr:
                ap.status, ap.decided_by, ap.reason = appr.get("decision", "approved"), appr.get("approver"), appr.get("reason")
                ap.decided_at = datetime.now(UTC)
                await repo.bump_interventions(ses, iid)
            elif result["phase"] == "Expired":
                ap.status = "expired"
            await repo.add_event(ses, iid, "approval_decided", f"Approval for {action['name']}: {ap.status}"
                                 + (f" by {ap.decided_by}" if ap.decided_by else ""), actor=ap.decided_by or "aegis-engine")
        return result

    async def _execute_and_verify(self, iid: str, action: dict[str, Any], cand: RemediationCandidate, dx: Diagnosis) -> str:
        before = dict(self.d.detector.latest)
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            await repo.set_status(ses, inc, IncidentStatus.EXECUTING,
                                  f"Executing {cand.action_type.value} on {cand.target.name}", {"action": action["name"]})
        with tracer.start_as_current_span("incident.execute", attributes={"aegis.action": action["name"]}):
            action = await self._await_action(iid, action, {"Succeeded", "Failed", "Denied", "Rejected", "Expired"},
                                              self.d.settings.verification_timeout_seconds + 240)
        if action["phase"] != "Succeeded":
            async with self.d.db.session() as ses:
                await repo.add_event(ses, iid, "action_failed", f"{action['name']} ended {action['phase']}: {action.get('message')}")
            if action["phase"] == "Failed":
                await self._follow_revert(iid, action["name"])
            return "failed"
        services = sorted(set(dx.affected_services) | ({dx.selected.component} if dx.selected else set())
                          | set(await self._affected(iid)))
        services = [s for s in services if s in self.d.detector.latest]
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            await repo.set_status(ses, inc, IncidentStatus.VERIFYING, f"Verifying recovery of {', '.join(services)}")
            await repo.add_event(ses, iid, "verification_started", "Observing SLOs after remediation",
                                 data={"services": services})
        with tracer.start_as_current_span("incident.verify"):
            ver = await self.d.verifier.verify(iid, action["name"], services, before)
        async with self.d.db.session() as ses:
            ses.add(VerificationRecord(id=ver.id, incident_id=iid, action_id=action["name"], outcome=ver.outcome.value,
                                       content=ver.model_dump(mode="json")))
            await repo.update_action(ses, action["name"], outcome=ver.outcome.value)
            await repo.add_event(ses, iid, "verification_completed", ver.summary,
                                 data={"outcome": ver.outcome.value, "failed": [c.model_dump() for c in ver.checks if not c.passed][:10]})
        match ver.outcome:
            case VerificationOutcome.RESOLVED:
                approved = bool((action.get("approval") or {}).get("decision") == "approved")
                await self._resolve(iid, "resolved_with_approval" if approved else "resolved_autonomously",
                                    f"Verified recovery after {cand.action_type.value} on {cand.target.name}")
                return "resolved"
            case VerificationOutcome.PARTIAL:
                return "partial"
            case _:
                if action.get("reversible"):
                    await self._revert(iid, action["name"], f"verification outcome {ver.outcome.value}: {ver.summary}")
                return "reverted"

    async def _revert(self, iid: str, action_name: str, why: str) -> None:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            await repo.set_status(ses, inc, IncidentStatus.ROLLED_BACK, f"Reverting {action_name}: {why}")
            await repo.add_event(ses, iid, "rollback_started", f"Requested revert of {action_name}")
        try:
            rv = await self.d.cp.revert(action_name, why[:1500])
        except (ControlPlaneError, UpstreamUnavailable) as exc:
            async with self.d.db.session() as ses:
                await repo.add_event(ses, iid, "rollback_failed", f"Revert of {action_name} refused: {exc}")
            raise Escalate(f"could not revert {action_name}: {exc}") from exc
        await self._sync_action(iid, rv)
        rv = await self._await_action(iid, rv, TERMINAL_ACTION, 300)
        async with self.d.db.session() as ses:
            ok = rv["phase"] == "Succeeded"
            await repo.add_event(ses, iid, "rollback_completed" if ok else "rollback_failed",
                                 f"Revert {rv['name']} {rv['phase']}: {rv.get('message', '')}")
        if rv["phase"] != "Succeeded":
            raise Escalate(f"revert {rv['name']} ended {rv['phase']}; manual intervention required")

    async def _follow_revert(self, iid: str, action_name: str) -> None:
        """The controller auto-reverts reversible failed actions; record the outcome."""
        try:
            rv = await self.d.cp.action(action_name + "-revert")
        except (ControlPlaneError, UpstreamUnavailable):
            return
        async with self.d.db.session() as ses:
            await repo.add_event(ses, iid, "rollback_started", f"Controller auto-revert {rv['name']} of failed {action_name}",
                                 actor="aegis-controller")
        await self._sync_action(iid, rv)
        rv = await self._await_action(iid, rv, TERMINAL_ACTION, 300)
        async with self.d.db.session() as ses:
            await repo.add_event(ses, iid, "rollback_completed" if rv["phase"] == "Succeeded" else "rollback_failed",
                                 f"Auto-revert {rv['name']} {rv['phase']}", actor="aegis-controller")

    async def _simulate(self, iid: str, cand: RemediationCandidate) -> tuple[str, str | None]:
        async with self.d.db.session() as ses:
            await repo.add_event(ses, iid, "simulation_started",
                                 f"Sandbox simulation of {cand.action_type.value} on {cand.target.name}")
        try:
            sim = await self.d.cp.submit_simulation({
                "incidentId": iid, "target": cand.target.model_dump(),
                "proposal": {"actionType": cand.action_type.value, "parameters": cand.params.wire()},
                "load": {"rps": 15, "durationSeconds": 15, "concurrency": 6}})
        except (ControlPlaneError, UpstreamUnavailable) as exc:
            async with self.d.db.session() as ses:
                await repo.add_event(ses, iid, "simulation_completed", f"Simulation unavailable: {exc}")
            return "Unavailable", None
        async with self.d.db.session() as ses:
            ses.add(SimulationRecord(id=sim["name"], incident_id=iid, candidate_id=cand.id, action_type=cand.action_type.value,
                                     target=cand.target.model_dump(), phase=sim["status"].get("phase", "Pending")))
        deadline = time.monotonic() + self.d.settings.simulation_timeout_seconds
        while sim["status"].get("phase") not in ("Completed", "Failed", "Unavailable") and time.monotonic() < deadline:
            try:
                sim = await self.d.cp.simulation(sim["name"], wait_for_version=sim["resourceVersion"], timeout=25)
            except (ControlPlaneError, UpstreamUnavailable):
                await asyncio.sleep(2)
        st = sim["status"]
        verdict = st.get("verdict") or st.get("phase", "Unknown")
        async with self.d.db.session() as ses:
            row = await ses.get(SimulationRecord, sim["name"])
            row.phase, row.verdict = st.get("phase", "Unknown"), st.get("verdict")
            row.baseline, row.candidate = st.get("baseline"), st.get("candidate")
            row.summary, row.reason = st.get("summary", ""), st.get("reason", "")
            row.completed_at = datetime.now(UTC)
            await repo.add_event(ses, iid, "simulation_completed",
                                 f"Simulation {sim['name']}: {st.get('phase')} {st.get('verdict') or ''} "
                                 f"{st.get('summary') or st.get('reason') or ''}".strip(),
                                 data={"verdict": st.get("verdict"), "baseline": st.get("baseline"),
                                       "candidate": st.get("candidate")})
        return verdict, sim["name"]

    async def _sync_action(self, iid: str, a: dict[str, Any], plan: RemediationPlan | None = None,
                           cand: RemediationCandidate | None = None) -> None:
        dec = a.get("decision") or {}
        async with self.d.db.session() as ses:
            prev = await ses.get(ActionRecord, a["name"])
            await repo.upsert_action(
                ses, a["name"], incident_id=iid, action_type=a["actionType"], target=a["target"],
                params=a.get("parameters") or {}, phase=a["phase"], risk_level=dec.get("riskLevel"),
                risk_score=dec.get("riskScore"), requires_approval=bool(dec.get("requiresApproval")), decision=dec,
                message=a.get("message", ""), revert_of=a.get("revertOf") or None, simulation_id=a.get("simulationRef") or None,
                plan_id=plan.id if plan else None, candidate_id=cand.id if cand else None,
                category=cand.addresses.value if cand else None, rationale=cand.rationale if cand else None,
                started_at=_ts(a.get("startedAt")), completed_at=_ts(a.get("completedAt")))
            if prev is None or prev.phase != a["phase"]:
                etype = {"Executing": "action_started", "Succeeded": "action_completed", "Failed": "action_failed",
                         "RolledBack": "action_rolled_back", "Denied": "action_denied"}.get(a["phase"], "action_updated")
                await repo.add_event(ses, iid, etype, f"{a['name']} ({a['actionType']} on {a['target']['name']}): "
                                     f"{a['phase']} — {a.get('message', '')}", actor="aegis-controller",
                                     data={"action": a["name"], "phase": a["phase"]})

    # ----------------------------------------------------- health / timing --
    def _recovered(self, services: list[str], cycles: int | None = None) -> bool:
        n = cycles or max(2, int(self.d.settings.self_recovery_seconds / self.d.settings.detect_interval_seconds))
        hist = list(self.d.detector.history)[-n:]
        if len(hist) < n:
            return False
        return all(snap.get(s) is not None and not snap[s].anomalies for snap in hist for s in services)

    async def _await_recovery(self, services: list[str], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self.d.detector.next_cycle(timeout=15)
            if self._recovered(services):
                return True
        return False

    async def _recurrence_watch(self, iid: str, services: list[str]) -> bool:
        self.watching.add(iid)
        try:
            deadline = time.monotonic() + self.d.settings.recurrence_watch_seconds
            streak = 0
            while time.monotonic() < deadline:
                await self.d.detector.next_cycle(timeout=15)
                latest = self.d.detector.latest
                bad = [s for s in services if latest.get(s) is not None and latest[s].anomalies]
                streak = streak + 1 if bad else 0
                if streak >= 3:
                    return True
            async with self.d.db.session() as ses:
                await repo.add_event(ses, iid, "recurrence_watch_passed",
                                     f"No recurrence during the {self.d.settings.recurrence_watch_seconds}s watch window")
            return False
        finally:
            self.watching.discard(iid)

    async def _affected(self, iid: str) -> list[str]:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            return list(inc.affected_services) if inc else []

    async def _history(self, affected: list[str]) -> list[dict[str, Any]]:
        async with self.d.db.session() as ses:
            rows = (await ses.execute(select(Incident).where(Incident.root_service.in_(affected),
                                                             Incident.status.in_(["RESOLVED", "ESCALATED"]))
                                      .order_by(Incident.detected_at.desc()).limit(5))).scalars().all()
            out = []
            for r in rows:
                acts = (await ses.execute(select(ActionRecord.action_type, ActionRecord.outcome)
                                          .where(ActionRecord.incident_id == r.id))).all()
                out.append({"id": r.id, "root_service": r.root_service, "category": r.category, "outcome": r.outcome,
                            "detected_at": r.detected_at.isoformat(),
                            "actions": [f"{t}:{o or 'n/a'}" for t, o in acts]})
            return out

    async def history_efficacy(self, category: str, action_type: str) -> tuple[int, int]:
        async with self.d.db.session() as ses:
            rows = (await ses.execute(select(ActionRecord.outcome, func.count()).where(
                ActionRecord.category == category, ActionRecord.action_type == action_type,
                ActionRecord.outcome.is_not(None)).group_by(ActionRecord.outcome))).all()
        ok = sum(n for o, n in rows if o == VerificationOutcome.RESOLVED.value)
        bad = sum(n for o, n in rows if o in (VerificationOutcome.NO_EFFECT.value, VerificationOutcome.DEGRADED.value))
        return ok, bad

    # ----------------------------------------------------------- terminals --
    async def _resolve(self, iid: str, outcome: str, message: str) -> None:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            if inc is None:
                return
            inc.outcome = outcome
            await repo.set_status(ses, inc, IncidentStatus.RESOLVED, message, {"outcome": outcome})
            await repo.add_event(ses, iid, "incident_resolved", f"Incident resolved ({outcome})")

    async def _escalate(self, iid: str, reason: str) -> None:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            if inc is None or inc.status == IncidentStatus.ESCALATED.value:
                return
            if inc.status == IncidentStatus.RESOLVED.value and inc.resolved_at is not None:
                return
            inc.outcome = "escalated"
            await repo.set_status(ses, inc, IncidentStatus.ESCALATED, f"Escalated to on-call: {reason}", {"reason": reason})
            await repo.add_event(ses, iid, "incident_escalated", reason)

    async def _finish(self, iid: str) -> None:
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            terminal = inc is not None and IncidentStatus(inc.status).terminal
            if terminal:
                await repo.close_pending_approvals(ses, "superseded", iid)
        if terminal:
            try:
                await self.d.postmortem.generate(iid)
            except Exception:
                log.exception("postmortem generation failed")

    # ---------------------------------------------------------- operators --
    async def resolve_manually(self, iid: str, user: str, note: str) -> None:
        task = self.tasks.get(iid)
        if task and not task.done():
            task.cancel()
        async with self.d.db.session() as ses:
            inc = await ses.get(Incident, iid)
            # Escalated incidents are owned by humans and closed by them; resolved ones stay closed.
            if inc is None or inc.status == IncidentStatus.RESOLVED.value:
                return
            inc.outcome = "resolved_manually"
            await repo.set_status(ses, inc, IncidentStatus.RESOLVED, f"Resolved manually by {user}: {note}", actor=user)
            await repo.bump_interventions(ses, iid)
        await self._finish(iid)


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _leading(dx: Diagnosis) -> str:
    known = [h for h in dx.hypotheses if h.category != CauseCategory.UNKNOWN]
    if not known:
        return "no hypothesis is supported by the evidence yet"
    h = known[0]
    return f"leading hypothesis {h.category.value} on {h.component} at {h.confidence:.0%}"
