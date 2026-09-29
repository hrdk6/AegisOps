"""Structured output contracts for model calls, plus semantic validators.

Schema validation (pydantic) rejects malformed output; the semantic validators
reject *grounding* violations: citing evidence that does not exist, naming
components outside the system, or proposing actions outside the registry. A
model can therefore never introduce new telemetry or new executable actions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis.domain.enums import ActionType, CauseCategory
from aegis.domain.schemas import ActionParams


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMHypothesis(Strict):
    category: CauseCategory
    component: str = Field(max_length=64)
    statement: str = Field(max_length=500)
    supporting_evidence: list[str] = Field(default_factory=list, max_length=12)
    contradicting_evidence: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)


class LLMDiagnosis(Strict):
    summary: str = Field(max_length=900)
    hypotheses: list[LLMHypothesis] = Field(min_length=1, max_length=5)
    uncertainty: str = Field(default="", max_length=700)


class LLMRemediationIdea(Strict):
    action_type: ActionType
    target: str = Field(max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(max_length=500)
    expected_effect: str = Field(max_length=300)


class LLMRemediation(Strict):
    candidates: list[LLMRemediationIdea] = Field(default_factory=list, max_length=4)


class LLMNarrative(Strict):
    summary: str = Field(max_length=1500)
    interpretation: str = Field(max_length=2000)
    lessons: list[str] = Field(default_factory=list, max_length=6)


def validate_diagnosis(d: LLMDiagnosis, evidence_ids: set[str], components: set[str]) -> list[str]:
    errors = []
    for i, h in enumerate(d.hypotheses):
        if h.component not in components:
            errors.append(f"hypotheses[{i}].component '{h.component}' is not a known service or data store")
        unknown = [e for e in h.supporting_evidence + h.contradicting_evidence if e not in evidence_ids]
        if unknown:
            errors.append(f"hypotheses[{i}] cites unknown evidence ids {unknown[:4]}; cite only ids from the evidence list")
        if not h.supporting_evidence and h.category != CauseCategory.UNKNOWN:
            errors.append(f"hypotheses[{i}] must cite at least one supporting evidence id")
    return errors


def validate_remediation(r: LLMRemediation, workloads: set[str]) -> list[str]:
    errors = []
    for i, c in enumerate(r.candidates):
        if c.target not in workloads:
            errors.append(f"candidates[{i}].target '{c.target}' is not a known workload")
        try:
            ActionParams.model_validate(c.params)
        except ValidationError as exc:
            errors.append(f"candidates[{i}].params invalid: {exc.errors()[0]['msg']}")
    return errors
