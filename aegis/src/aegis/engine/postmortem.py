"""Structured postmortem generation.

Every statement is typed:
  FACT                  derived from system records (timeline, actions, telemetry queries)
  HYPOTHESIS            the diagnosis, always with its confidence
  MODEL_INTERPRETATION  narrative produced by a model (only when one is configured) or by
                        deterministic templates (labelled as such)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aegis.ai.contracts import LLMNarrative
from aegis.ai.prompts import NARRATIVE_SYSTEM, narrative_prompt
from aegis.ai.router import ModelRouter
from aegis.clients.http import UpstreamUnavailable
from aegis.clients.prometheus import Prometheus
from aegis.db import repo
from aegis.db.models import (
    ActionRecord,
    ApprovalRecord,
    DiagnosisRecord,
    Event,
    EvidenceRecord,
    Incident,
    Postmortem,
    SimulationRecord,
    VerificationRecord,
)
from aegis.db.session import Database
from aegis.domain.enums import ClaimType
from aegis.domain.schemas import Claim, PostmortemSection
from aegis.knowledge import runbooks as kb

log = logging.getLogger("aegis.postmortem")

PREVENTION: dict[str, list[str]] = {
    "BAD_DEPLOYMENT": ["Route releases of this service through a CanaryRelease with SLO gates before full rollout.",
                       "Add a regression test for the failing code path to the release pipeline."],
    "CONFIG_ERROR": ["Validate configuration schemas in CI and reject invalid ConfigMaps at admission time.",
                     "Reference versioned, immutable ConfigMaps from the pod template so config changes roll out like code."],
    "RESOURCE_MISCONFIGURATION": ["Require review for resource-limit reductions; derive limits from observed usage plus headroom."],
    "CPU_SATURATION": ["Add a HorizontalPodAutoscaler for this CPU-bound service and load-test per-replica capacity."],
    "MEMORY_EXHAUSTION": ["Bound in-process caches and alert on sustained working-set growth before OOM."],
    "DB_CONNECTION_EXHAUSTION": ["Enforce idle-in-transaction and statement timeouts; test connection release on error paths."],
    "DEPENDENCY_FAILURE": ["Protect shared dependencies from scale-to-zero with admission policy and PodDisruptionBudgets."],
    "NETWORK_DEGRADATION": ["Alert on client/server latency divergence per edge and hand off to the network owner automatically."],
}


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


class PostmortemGenerator:
    def __init__(self, db: Database, prom: Prometheus, router: ModelRouter | None, runbook_cache: list[kb.Runbook]) -> None:
        self.db = db
        self.prom = prom
        self.router = router
        self.runbooks = runbook_cache

    async def _impact(self, start: datetime, end: datetime) -> dict[str, Any] | None:
        dur = max(30, int((end - start).total_seconds()))
        at = end.timestamp()
        try:
            errs = await self.prom.vector(f'sum(increase(loadgen_requests_total{{outcome="error"}}[{dur}s] @ {at:.0f}))')
            total = await self.prom.vector(f'sum(increase(loadgen_requests_total[{dur}s] @ {at:.0f}))')
        except UpstreamUnavailable:
            return None
        e = errs[0][1] if errs else 0.0
        t = total[0][1] if total else 0.0
        return {"failed_requests": round(e), "total_requests": round(t), "failure_ratio": round(e / t, 4) if t else 0.0,
                "window_seconds": dur}

    async def generate(self, incident_id: str) -> dict[str, Any]:
        async with self.db.session() as s:
            inc = await s.get(Incident, incident_id)
            if inc is None:
                raise ValueError(f"incident {incident_id} not found")
            events = list((await s.execute(select(Event).where(Event.incident_id == incident_id).order_by(Event.id))).scalars())
            dx_row = (await s.execute(select(DiagnosisRecord).where(DiagnosisRecord.incident_id == incident_id)
                                      .order_by(DiagnosisRecord.created_at.desc()).limit(1))).scalar_one_or_none()
            actions = list((await s.execute(select(ActionRecord).where(ActionRecord.incident_id == incident_id)
                                            .order_by(ActionRecord.created_at))).scalars())
            verifs = list((await s.execute(select(VerificationRecord).where(VerificationRecord.incident_id == incident_id)
                                           .order_by(VerificationRecord.created_at))).scalars())
            sims = list((await s.execute(select(SimulationRecord).where(SimulationRecord.incident_id == incident_id))).scalars())
            approvals = list((await s.execute(select(ApprovalRecord).where(ApprovalRecord.incident_id == incident_id))).scalars())
            evidence = {e.id: e for e in (await s.execute(select(EvidenceRecord).where(
                EvidenceRecord.incident_id == incident_id))).scalars()}

        dx = dx_row.content if dx_row else {}
        selected = dx.get("selected")
        end = inc.resolved_at or datetime.now(UTC)
        onset = inc.onset_at or inc.detected_at
        duration = (end - onset).total_seconds()
        impact = await self._impact(onset, end)
        sections: list[PostmortemSection] = []
        F, H, INTERP = ClaimType.FACT, ClaimType.HYPOTHESIS, ClaimType.INTERPRETATION

        summary = [Claim(type=F, text=f"{inc.id} ({inc.severity}) affected {', '.join(inc.affected_services) or 'n/a'}; "
                                      f"status {inc.status}, outcome {inc.outcome or 'n/a'}, duration {_fmt_dur(duration)} "
                                      f"from anomaly onset.")]
        if selected:
            summary.append(Claim(type=H, text=f"Root cause ({selected['confidence']:.0%} confidence): {selected['category']} on "
                                              f"{selected['component']} — {selected['statement']}",
                                 evidence=selected.get("supporting_evidence", [])[:6]))
        sections.append(PostmortemSection(title="Summary", claims=summary))

        impact_claims = []
        if impact:
            impact_claims.append(Claim(type=F, text=(
                f"Synthetic customers observed {impact['failed_requests']} failed requests out of {impact['total_requests']} "
                f"({impact['failure_ratio']:.1%}) between onset and resolution.")))
        else:
            impact_claims.append(Claim(type=F, text="Customer-impact metrics were unavailable for this window."))
        impact_claims.append(Claim(type=F, text=f"Services with SLO violations: {', '.join(inc.symptoms and sorted({x.get('service') for x in inc.symptoms if isinstance(x, dict)}) or inc.affected_services)}."))
        sections.append(PostmortemSection(title="Customer and system impact", claims=impact_claims))

        detect = (inc.detected_at - onset).total_seconds() if inc.onset_at else None
        symptoms = [f"{x.get('service')} {x.get('signal')} ({x.get('reason')})" for x in (inc.symptoms or []) if isinstance(x, dict)]
        sections.append(PostmortemSection(title="Detection", claims=[
            Claim(type=F, text=f"Anomaly onset {onset:%H:%M:%S}Z; incident created {inc.detected_at:%H:%M:%S}Z "
                               f"(time to detect {_fmt_dur(detect)})."),
            Claim(type=F, text="Triggering symptoms: " + ("; ".join(symptoms[:6]) or "n/a")),
        ]))

        timeline = [Claim(type=F, text=f"{e.ts:%H:%M:%S} [{e.type}] {e.message}") for e in events
                    if e.type not in ("evidence_collected",)][:60]
        sections.append(PostmortemSection(title="Timeline", claims=timeline))

        rc_claims = []
        if selected:
            rc_claims.append(Claim(type=H, text=f"{selected['category']} on {selected['component']} "
                                                f"(confidence {selected['confidence']:.0%}, method {dx.get('method')}).",
                                   evidence=selected.get("supporting_evidence", [])))
            for eid in selected.get("supporting_evidence", [])[:8]:
                ev = evidence.get(eid)
                if ev:
                    rc_claims.append(Claim(type=F, text=f"[{eid}] {ev.summary}", evidence=[eid]))
            for eid in selected.get("contradicting_evidence", [])[:4]:
                ev = evidence.get(eid)
                if ev:
                    rc_claims.append(Claim(type=F, text=f"Contradicting: [{eid}] {ev.summary}", evidence=[eid]))
        else:
            rc_claims.append(Claim(type=H, text="No hypothesis reached the confidence threshold; root cause undetermined."))
        sections.append(PostmortemSection(title="Root cause and evidence", claims=rc_claims))

        others = [h for h in dx.get("hypotheses", []) if not selected or
                  (h["category"], h["component"]) != (selected["category"], selected["component"])]
        sections.append(PostmortemSection(title="Contributing factors and alternatives", claims=[
            Claim(type=H, text=f"{h['category']} on {h['component']} ({h['confidence']:.0%}): {h['statement']}",
                  evidence=h.get("supporting_evidence", [])[:4]) for h in others if h["confidence"] >= 0.05][:4]
            or [Claim(type=F, text="No material alternative hypotheses.")]))

        rem = []
        for a in actions:
            dec = a.decision or {}
            rem.append(Claim(type=F, text=(
                f"{a.action_type} on {a.target.get('name')} {a.params or ''}: phase {a.phase}; policy risk {a.risk_level} "
                f"(score {a.risk_score}); approval {'required' if a.requires_approval else 'not required'}"
                f"{'; revert of ' + a.revert_of if a.revert_of else ''}; verification {a.outcome or 'n/a'}. "
                f"{('Policy: ' + '; '.join(dec.get('reasons', [])[:2])) if dec.get('reasons') else ''}")))
        for ap in approvals:
            rem.append(Claim(type=F, text=f"Approval for {ap.id}: {ap.status} by {ap.decided_by or 'n/a'}"
                                          f"{' — ' + ap.reason if ap.reason else ''}."))
        for sim in sims:
            rem.append(Claim(type=F, text=f"Sandbox simulation {sim.id} ({sim.action_type}): {sim.phase}, verdict "
                                          f"{sim.verdict or 'n/a'}. {sim.summary or sim.reason}"))
        sections.append(PostmortemSection(title="Remediation", claims=rem or [Claim(type=F, text="No remediation executed.")]))

        sections.append(PostmortemSection(title="Verification", claims=[
            Claim(type=F, text=f"{v.action_id}: {v.content.get('summary', v.outcome)}") for v in verifs]
            or [Claim(type=F, text="No verification performed.")]))

        rollbacks = [a for a in actions if a.revert_of or a.phase == "RolledBack"]
        sections.append(PostmortemSection(title="Rollback", claims=[
            Claim(type=F, text=(f"{a.id} reverted {a.revert_of} ({a.phase})" if a.revert_of else f"{a.id} was rolled back"))
            for a in rollbacks] or [Claim(type=F, text="No interventions were rolled back.")]))

        missed = []
        for ev in evidence.values():
            if ev.kind == "change" and (ev.data or {}).get("secondsBeforeOnset", -1) >= 0:
                missed.append(Claim(type=INTERP, text=(
                    f"[template] {ev.title}: the change reached production without a gate that detected the regression."),
                    evidence=[ev.id]))
        if detect is not None and detect > 45:
            missed.append(Claim(type=INTERP, text=f"[template] Detection took {_fmt_dur(detect)} after onset; tighter "
                                             "burn-rate alerting could shorten it."))
        if selected and selected["category"] == "BAD_DEPLOYMENT" and not any(e.kind == "canary" for e in evidence.values()):
            missed.append(Claim(type=INTERP, text="[template] The release went to 100% of traffic at once; a canary would have "
                                             "limited the blast radius."))
        sections.append(PostmortemSection(title="Missed signals", claims=missed[:5] or [
            Claim(type=INTERP, text="[template] No missed signals identified.")]))

        prevention = [Claim(type=INTERP, text=f"[template] {p}") for p in PREVENTION.get(selected["category"] if selected else "", [])]
        if selected:
            for rb in kb.search_local(self.runbooks, selected["category"], selected.get("statement", ""), limit=2):
                prevention.append(Claim(type=INTERP, text=f"Runbook {rb['id']} ({rb['path']}): {rb['title']}"))
        sections.append(PostmortemSection(title="Recommended preventive actions", claims=prevention or [
            Claim(type=INTERP, text="[template] Review this incident class manually.")]))

        method = "template"
        if self.router is not None and self.router.available("postmortem"):
            facts = [c.text for sec in sections for c in sec.claims if c.type == F][:40]
            hyps = [c.text for sec in sections for c in sec.claims if c.type == H][:5]
            res = await self.router.structured("postmortem", NARRATIVE_SYSTEM, narrative_prompt(facts, hyps), LLMNarrative,
                                               incident_id=incident_id, max_tokens=900, temperature=0.2)
            if res.value:
                method = f"template+model:{res.provider}:{res.model}"
                claims = [Claim(type=INTERP, text=res.value.summary), Claim(type=INTERP, text=res.value.interpretation)]
                claims += [Claim(type=INTERP, text=f"Lesson: {lesson}") for lesson in res.value.lessons]
                sections.insert(1, PostmortemSection(title="Narrative (model-generated interpretation)", claims=claims))

        content = {"incident_id": inc.id, "title": inc.title, "severity": inc.severity, "status": inc.status,
                   "outcome": inc.outcome, "onset": onset.isoformat(), "detected_at": inc.detected_at.isoformat(),
                   "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None, "duration_seconds": duration,
                   "impact": impact, "method": method, "sections": [sec.model_dump(mode="json") for sec in sections],
                   "generated_at": datetime.now(UTC).isoformat()}
        md = self.render(content)
        async with self.db.session() as s:
            stmt = pg_insert(Postmortem).values(incident_id=inc.id, content=content, markdown=md, method=method,
                                                generated_at=datetime.now(UTC))
            stmt = stmt.on_conflict_do_update(index_elements=["incident_id"],
                                              set_={"content": content, "markdown": md, "method": method,
                                                    "generated_at": datetime.now(UTC)})
            await s.execute(stmt)
            await repo.add_event(s, inc.id, "postmortem_generated", f"Postmortem generated ({method})")
        return content

    @staticmethod
    def render(c: dict[str, Any]) -> str:
        badge = {"FACT": "**[FACT]**", "HYPOTHESIS": "**[HYPOTHESIS]**", "MODEL_INTERPRETATION": "*[INTERPRETATION]*"}
        lines = [f"# Postmortem: {c['incident_id']} — {c['title']}", "",
                 f"**Severity:** {c['severity']} · **Status:** {c['status']} · **Outcome:** {c.get('outcome') or 'n/a'} · "
                 f"**Duration:** {_fmt_dur(c.get('duration_seconds'))} · **Generated by:** {c['method']}", "",
                 "> Legend: **[FACT]** verified from system records and telemetry · **[HYPOTHESIS]** diagnosis with "
                 "stated confidence · *[INTERPRETATION]* generated narrative or template recommendation (not verified).", ""]
        for sec in c["sections"]:
            lines.append(f"## {sec['title']}")
            lines.append("")
            for claim in sec["claims"]:
                ev = f" _(evidence: {', '.join(claim['evidence'][:4])})_" if claim.get("evidence") else ""
                lines.append(f"- {badge.get(claim['type'], '')} {claim['text']}{ev}")
            lines.append("")
        return "\n".join(lines)
