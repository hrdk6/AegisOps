"""Diagnosis engine: deterministic hypotheses, optional model refinement, fusion.

    rules  ──► calibrated hypotheses ──┐
                                       ├─► fusion ─► Diagnosis (evidence-linked)
    model (optional, validated) ───────┘

The model can re-rank, re-word and add hypotheses, but every hypothesis it
contributes must cite existing evidence, and a hypothesis supported only by
the model is capped so the model cannot dominate the deterministic analysis.
If the model is unavailable or produces invalid output, the rule-based
diagnosis stands unchanged (graceful degradation).
"""

from __future__ import annotations

import math
import secrets
from datetime import UTC, datetime

from aegis.ai.contracts import LLMDiagnosis, validate_diagnosis
from aegis.ai.prompts import DIAGNOSIS_SYSTEM, diagnosis_prompt
from aegis.ai.router import ModelRouter
from aegis.domain.enums import CauseCategory
from aegis.domain.schemas import Diagnosis, Hypothesis
from aegis.engine.context import ContextBundle
from aegis.engine.diagnosis import rules

TEMPERATURE = 2.0
UNKNOWN_SCORE = 2.0
MODEL_WEIGHT = 0.4
MODEL_ONLY_CAP = 0.6
MIN_SELECT_CONFIDENCE = 0.35


def calibrate(cands: list[rules.Candidate], fallback_component: str) -> list[Hypothesis]:
    """Tempered softmax over rule scores with an explicit UNKNOWN alternative."""
    pool = [(c.score, c) for c in cands] + [(UNKNOWN_SCORE, None)]
    mx = max(s for s, _ in pool)
    weights = [math.exp((s - mx) / TEMPERATURE) for s, _ in pool]
    total = sum(weights)
    out: list[Hypothesis] = []
    for (score, c), w in zip(pool, weights, strict=True):
        conf = round(w / total, 4)
        if c is None:
            out.append(Hypothesis(category=CauseCategory.UNKNOWN, component=fallback_component, confidence=conf,
                                  statement="No hypothesis is strongly supported by the available evidence",
                                  rule_score=score))
        else:
            out.append(Hypothesis(category=c.category, component=c.component, statement=c.statement,
                                  supporting_evidence=c.supporting, contradicting_evidence=c.contradicting,
                                  confidence=conf, trigger_change=c.trigger_change, edge=c.edge, rule_score=round(score, 2)))
    return sorted(out, key=lambda h: h.confidence, reverse=True)


def fuse(rule_hyps: list[Hypothesis], model: LLMDiagnosis) -> list[Hypothesis]:
    by_key = {(h.category, h.component): h.model_copy() for h in rule_hyps}
    model_conf = {(h.category, h.component): h for h in model.hypotheses}
    fused: list[Hypothesis] = []
    for key in set(by_key) | set(model_conf):
        r = by_key.get(key)
        m = model_conf.get(key)
        rc = r.confidence if r else 0.0
        mc = m.confidence if m else 0.0
        conf = (1 - MODEL_WEIGHT) * rc + MODEL_WEIGHT * mc
        if r is None and m is not None:
            conf = min(conf, MODEL_ONLY_CAP * mc)
            fused.append(Hypothesis(category=m.category, component=m.component, statement=m.statement,
                                    supporting_evidence=m.supporting_evidence,
                                    contradicting_evidence=m.contradicting_evidence, confidence=round(conf, 4),
                                    source="model"))
        elif r is not None:
            h = r
            h.confidence = round(conf, 4)
            if m is not None:
                h.source = "rules+model"
                h.supporting_evidence = list(dict.fromkeys(h.supporting_evidence + m.supporting_evidence))
                h.contradicting_evidence = list(dict.fromkeys(h.contradicting_evidence + m.contradicting_evidence))
            fused.append(h)
    total = sum(h.confidence for h in fused) or 1.0
    for h in fused:
        h.confidence = round(h.confidence / total, 4)
    return sorted(fused, key=lambda h: h.confidence, reverse=True)


class DiagnosisEngine:
    def __init__(self, router: ModelRouter | None = None) -> None:
        self.router = router

    async def diagnose(self, incident_id: str, title: str, bundle: ContextBundle, iteration: int) -> Diagnosis:
        cands = rules.generate(bundle)
        roots = bundle.graph.root_candidates(bundle.anomalous)
        fallback = roots[0] if roots else (sorted(bundle.anomalous)[0] if bundle.anomalous else "unknown")
        hyps = calibrate(cands, fallback)
        method = "rules"
        meta: dict = {"rule_factors": {f"{c.category.value}:{c.component}": c.factors for c in cands[:6]}}
        model_summary = None
        if self.router is not None and self.router.available("diagnosis"):
            ids = {e.id for e in bundle.evidence}
            components = set(bundle.graph.nodes) | set(bundle.signals)
            prompt = diagnosis_prompt(title, sorted(bundle.anomalous), roots, sorted(components), hyps,
                                      bundle.ranked(40), bundle.gaps)
            res = await self.router.structured("diagnosis", DIAGNOSIS_SYSTEM, prompt, LLMDiagnosis, incident_id=incident_id,
                                               validate=lambda d: validate_diagnosis(d, ids, components))
            meta["model"] = res.metadata
            if res.value is not None:
                hyps = fuse(hyps, res.value)
                method = "rules+model"
                model_summary = res.value.summary
                meta["model_uncertainty"] = res.value.uncertainty
        top = hyps[0] if hyps else None
        selected = top if top and top.category != CauseCategory.UNKNOWN and top.confidence >= MIN_SELECT_CONFIDENCE else None
        blast = bundle.graph.upstream(selected.component) if selected else []
        runner_up = hyps[1] if len(hyps) > 1 else None
        uncertainty = []
        if bundle.gaps:
            uncertainty.append("Missing telemetry: " + "; ".join(bundle.gaps))
        if selected and runner_up and selected.confidence - runner_up.confidence < 0.15:
            uncertainty.append(f"Close alternative: {runner_up.category.value} on {runner_up.component} "
                               f"({runner_up.confidence:.0%})")
        if selected and selected.contradicting_evidence:
            uncertainty.append(f"{len(selected.contradicting_evidence)} evidence item(s) contradict the selected hypothesis")
        if not selected:
            uncertainty.append("No hypothesis reached the confidence required for autonomous remediation")
        if selected:
            summary = (f"Most likely root cause ({selected.confidence:.0%}): {selected.category.value} on "
                       f"{selected.component}. {selected.statement}.")
        else:
            summary = "Root cause undetermined with sufficient confidence; human investigation required."
        if model_summary:
            meta["model_summary"] = model_summary
        return Diagnosis(
            id=f"dx-{secrets.token_hex(5)}", incident_id=incident_id, iteration=iteration, summary=summary,
            hypotheses=hyps[:6], selected=selected, confidence=selected.confidence if selected else (top.confidence if top else 0.0),
            affected_services=sorted(bundle.anomalous), blast_radius=blast,
            uncertainty=" ".join(uncertainty) or "Evidence is consistent; no major gaps detected.",
            method=method, model_metadata=meta, created_at=datetime.now(UTC))
