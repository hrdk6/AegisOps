"""Remediation planning.

Candidates come from a deterministic playbook keyed on the diagnosed cause
(and, optionally, validated model suggestions). Each candidate is scored with
a historical efficacy prior, then dry-run against the control plane's
deterministic policy engine. Selection maximizes expected utility:

    utility = P(success) x risk_factor(level) x approval_factor

where P(success) = efficacy x hypothesis confidence. Candidates the policy
denies are never selected. When nothing viable remains the plan carries an
escalation reason instead of a selection.
"""

from __future__ import annotations

import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aegis.ai.contracts import LLMRemediation, validate_remediation
from aegis.ai.prompts import PLANNING_SYSTEM, planning_prompt
from aegis.ai.router import ModelRouter
from aegis.clients.controlplane import ControlPlane, ControlPlaneError
from aegis.clients.http import UpstreamUnavailable
from aegis.domain.enums import ActionType, CauseCategory
from aegis.domain.schemas import ActionParams, Diagnosis, Hypothesis, RemediationCandidate, RemediationPlan, Target
from aegis.engine.context import ContextBundle

log = logging.getLogger("aegis.planner")

SIMULATABLE = {ActionType.ROLLBACK_DEPLOYMENT, ActionType.PATCH_RESOURCES, ActionType.UPDATE_CONFIG}
RISK_FACTOR = {"LOW": 1.0, "MEDIUM": 0.9, "HIGH": 0.7, "CRITICAL": 0.4}
APPROVAL_FACTOR = 0.95
PRIOR_STRENGTH = 4.0

HistoryLookup = Callable[[str, str], Awaitable[tuple[int, int]]]


def _new_id() -> str:
    return "rc-" + secrets.token_hex(4)


def previous_revision(w: dict[str, Any]) -> int | None:
    """Newest recorded revision older than the current one (what a rollback restores)."""
    current = w.get("revision") or 0
    older = [r["revision"] for r in w.get("revisions", []) if 0 < r.get("revision", 0) < current]
    return max(older) if older else None


def owning_workload(b: ContextBundle, pod: str) -> dict[str, Any]:
    """Deployment owning a pod, by the longest workload-name prefix (pods are named <deployment>-<rs>-<id>)."""
    owners = [w for name, w in b.workloads.items() if pod.startswith(name + "-")]
    return max(owners, key=lambda w: len(w.get("name", "")), default={})


class Playbook:
    """Maps a hypothesis to concrete, parameterized candidate actions."""

    def __init__(self, namespace: str) -> None:
        self.ns = namespace

    def _cand(self, h: Hypothesis, b: ContextBundle, action: ActionType, target: str, efficacy: float, rationale: str,
              effect: str, rollback: str, params: ActionParams | None = None, prereq: list[str] | None = None,
              kind: str = "Deployment") -> RemediationCandidate:
        w = b.workloads.get(target, {})
        owner = w if kind == "Deployment" else owning_workload(b, target) if kind == "Pod" else {}
        return RemediationCandidate(
            id=_new_id(), action_type=action, target=Target(kind=kind, namespace=self.ns, name=target),  # type: ignore[arg-type]
            params=params or ActionParams(), rationale=rationale, expected_effect=effect,
            affected_components=[target] + b.graph.upstream(target), rollback_strategy=rollback,
            prerequisites=prereq or [], efficacy=efficacy, confidence=round(efficacy * h.confidence, 4),
            addresses=h.category, simulatable=action in SIMULATABLE and bool(w.get("simulationEnabled")),
            expected_revision=owner.get("revision") or None)

    def _trigger(self, h: Hypothesis, b: ContextBundle) -> dict[str, Any] | None:
        ev = b.by_id(h.trigger_change) if h.trigger_change else None
        return ev.data if ev else None

    def candidates(self, h: Hypothesis, b: ContextBundle) -> list[RemediationCandidate]:
        svc = h.component
        w = b.workloads.get(svc, {})
        trigger = self._trigger(h, b)
        has_prev_revision = len(w.get("revisions", [])) >= 2
        rb_rollback = "controller snapshot of the current pod template; revert_action restores it"
        out: list[RemediationCandidate] = []

        def rollback(eff: float, why: str) -> None:
            prev = previous_revision(w)
            if has_prev_revision and prev is not None:
                out.append(self._cand(h, b, ActionType.ROLLBACK_DEPLOYMENT, svc, eff, why,
                                      f"{svc} returns to revision {prev}; symptoms introduced by the change disappear",
                                      rb_rollback, params=ActionParams(toRevision=prev),
                                      prereq=[f"revision {prev} still in rollout history", "rollout not paused",
                                              f"{svc} still at revision {w.get('revision')} (checked by the controller)"]))

        match h.category:
            case CauseCategory.BAD_DEPLOYMENT:
                active = [c for c in b.canaries if c["spec"]["targetRef"] == svc
                          and c.get("status", {}).get("phase") in ("Progressing", "Paused", "Pending")]
                if active:
                    out.append(self._cand(h, b, ActionType.ABORT_CANARY, active[0]["name"], 0.95,
                                          f"The failing release is still a canary; aborting returns all traffic to stable {svc}",
                                          "canary pods removed, stable pods serve 100% of traffic",
                                          "not reversible (re-run the release to retry)", kind="CanaryRelease"))
                    out[-1].affected_components = [svc] + b.graph.upstream(svc)
                if not any(c for c in b.canaries if c["spec"]["targetRef"] == svc):
                    rollback(0.9, f"The regression started right after a deployment of {svc}; rolling back removes the change")
                if not w.get("rolloutComplete", True):
                    out.append(self._cand(h, b, ActionType.PAUSE_ROLLOUT, svc, 0.3,
                                          "Pause the in-progress rollout to stop further pods from being replaced",
                                          "limits blast radius; does not remove already-updated pods",
                                          "resume_rollout (snapshot restores paused=false)"))
            case CauseCategory.CONFIG_ERROR:
                if trigger and trigger.get("kind") == "ConfigMap":
                    change = b.by_id(h.trigger_change or "")
                    name = change.service if change else None  # change evidence is keyed by object name
                    if name:
                        out.append(self._cand(h, b, ActionType.UPDATE_CONFIG, svc, 0.9,
                                              f"ConfigMap {name} changed right before {svc} started failing; restoring the "
                                              "previous recorded revision and restarting consumers removes the bad configuration",
                                              f"{svc} pods start with valid configuration and become ready",
                                              "controller snapshot of the ConfigMap data; revert_action restores it",
                                              params=ActionParams(configMap=name, configRevision="previous"),
                                              prereq=["previous ConfigMap revision recorded by the controller"]))
                else:
                    rollback(0.85, f"An environment change in {svc}'s pod template left it with invalid configuration")
            case CauseCategory.RESOURCE_MISCONFIGURATION:
                restore = self._restore_resources(trigger)
                if restore:
                    container, patch = restore
                    out.append(self._cand(h, b, ActionType.PATCH_RESOURCES, svc, 0.85,
                                          f"Resource limits of {svc} were lowered; restoring the previous values",
                                          f"{svc} stops being OOMKilled/throttled", rb_rollback,
                                          params=ActionParams(container=container, resources=patch)))
                rollback(0.8, f"The previous revision of {svc} carries the previous resource settings")
            case CauseCategory.CPU_SATURATION:
                if trigger and trigger.get("kind") == "Deployment" and trigger.get("type") in ("release", "env"):
                    rollback(0.8, f"CPU usage of {svc} rose after a release that made requests more expensive")
                desired = max(1, int(w.get("replicas") or 1))
                util = (b.signals.get(svc).cpu_util if b.signals.get(svc) else None) or 1.0
                factor = min(3.0, max(1.5, util / 0.6))
                replicas = min(6, max(desired + 1, math.ceil(desired * factor)))
                out.append(self._cand(h, b, ActionType.SCALE_DEPLOYMENT, svc, 0.8 if not trigger else 0.5,
                                      f"{svc} is CPU-bound at {util:.0%} of its limit; scaling {desired}->{replicas} "
                                      f"replicas targets ~60% utilization",
                                      "CPU per replica drops, queueing and p95 latency recover",
                                      "controller snapshot of the replica count; revert_action restores it",
                                      params=ActionParams(replicas=replicas)))
            case CauseCategory.MEMORY_EXHAUSTION:
                if trigger and trigger.get("kind") == "Deployment":
                    rollback(0.85, f"Memory growth of {svc} began after a deployment (likely leak)")
                out.append(self._cand(h, b, ActionType.ROLLOUT_RESTART, svc, 0.35,
                                      f"Restarting {svc} releases leaked memory (temporary mitigation)",
                                      "memory resets; recurrence expected if the leak persists", "not reversible (restart)"))
            case CauseCategory.DB_CONNECTION_EXHAUSTION:
                if trigger and trigger.get("kind") == "Deployment":
                    rollback(0.85, f"Connection leakage in {svc} began after a deployment")
                out.append(self._cand(h, b, ActionType.ROLLOUT_RESTART, svc, 0.5,
                                      f"Restarting {svc} releases leaked database connections",
                                      "pool utilization drops; recurrence expected if the leak persists",
                                      "not reversible (restart)"))
            case CauseCategory.DEPENDENCY_FAILURE:
                desired = int(w.get("replicas") or 0)
                if desired == 0:
                    prior = self._previous_replicas(trigger) or max(1, int(b.signals.get(svc).desired if b.signals.get(svc) else 1), 1)
                    out.append(self._cand(h, b, ActionType.SCALE_DEPLOYMENT, svc, 0.9,
                                          f"{svc} has zero replicas; restoring the previous replica count ({prior})",
                                          f"{svc} becomes available; dependent services recover",
                                          "controller snapshot of the replica count; revert_action restores it",
                                          params=ActionParams(replicas=prior)))
                elif trigger and trigger.get("kind") == "Deployment":
                    rollback(0.75, f"{svc} became unavailable after a deployment")
                else:
                    out.append(self._cand(h, b, ActionType.ROLLOUT_RESTART, svc, 0.3,
                                          f"Restart {svc} to recover from a failed state", f"{svc} pods recreated",
                                          "not reversible (restart)"))
            case CauseCategory.POD_CRASHLOOP:
                if trigger:
                    rollback(0.6, f"{svc} started crash-looping after a change")
            case _:
                pass
        return out

    @staticmethod
    def _restore_resources(trigger: dict[str, Any] | None) -> tuple[str, dict[str, str]] | None:
        if not trigger:
            return None
        patch: dict[str, str] = {}
        container = ""
        for f in trigger.get("fields", []):
            parts = f.get("path", "").split(".")
            if len(parts) == 5 and parts[0] == "containers" and parts[2] == "resources" and f.get("old"):
                container = parts[1]
                key = {("limits", "cpu"): "cpuLimit", ("limits", "memory"): "memoryLimit",
                       ("requests", "cpu"): "cpuRequest", ("requests", "memory"): "memoryRequest"}.get((parts[3], parts[4]))
                if key:
                    patch[key] = f["old"]
        return (container, patch) if patch else None

    @staticmethod
    def _previous_replicas(trigger: dict[str, Any] | None) -> int | None:
        for f in (trigger or {}).get("fields", []):
            if f.get("path") == "spec.replicas":
                try:
                    return max(1, int(f.get("old") or 0))
                except ValueError:
                    return None
        return None


class Planner:
    def __init__(self, cp: ControlPlane, namespace: str, router: ModelRouter | None = None,
                 history: HistoryLookup | None = None) -> None:
        self.cp = cp
        self.namespace = namespace
        self.playbook = Playbook(namespace)
        self.router = router
        self.history = history

    async def _efficacy(self, c: RemediationCandidate) -> float:
        if self.history is None:
            return c.efficacy
        successes, failures = await self.history(c.addresses.value, c.action_type.value)
        return (c.efficacy * PRIOR_STRENGTH + successes) / (PRIOR_STRENGTH + successes + failures)

    async def _model_candidates(self, d: Diagnosis, b: ContextBundle) -> list[RemediationCandidate]:
        if self.router is None or d.selected is None or not self.router.available("planning"):
            return []
        workloads = set(b.workloads)
        res = await self.router.structured("planning", PLANNING_SYSTEM,
                                           planning_prompt(d.summary, d.selected, sorted(workloads), b.ranked(15)),
                                           LLMRemediation, incident_id=d.incident_id,
                                           validate=lambda r: validate_remediation(r, workloads))
        out = []
        for idea in res.value.candidates if res.value else []:
            h = d.selected
            out.append(RemediationCandidate(
                id=_new_id(), action_type=idea.action_type, target=Target(namespace=self.namespace, name=idea.target),
                params=ActionParams.model_validate(idea.params), rationale=f"[model suggestion] {idea.rationale}",
                expected_effect=idea.expected_effect, affected_components=[idea.target] + b.graph.upstream(idea.target),
                rollback_strategy="controller snapshot (if the action is reversible)", prerequisites=[],
                efficacy=0.5, confidence=round(0.5 * h.confidence, 4), addresses=h.category, source="model",
                simulatable=idea.action_type in SIMULATABLE and bool(b.workloads.get(idea.target, {}).get("simulationEnabled")),
                expected_revision=b.workloads.get(idea.target, {}).get("revision") or None))
        return out

    async def plan(self, d: Diagnosis, b: ContextBundle, exclude: set[str] | None = None) -> RemediationPlan:
        exclude = exclude or set()
        plan = RemediationPlan(id="plan-" + secrets.token_hex(4), incident_id=d.incident_id, diagnosis_id=d.id,
                               candidates=[], selected_index=None, created_at=datetime.now(UTC))
        if d.selected is None:
            plan.escalation_reason = "diagnosis confidence below the autonomy threshold"
            return plan
        hyps = [d.selected] + [h for h in d.hypotheses if h is not d.selected and h.confidence >= 0.2
                               and h.category != CauseCategory.UNKNOWN][:1]
        cands: list[RemediationCandidate] = []
        for h in hyps:
            cands += self.playbook.candidates(h, b)
        cands += await self._model_candidates(d, b)
        seen: set[str] = set()
        unique = []
        for c in cands:
            fp = c.fingerprint()
            if fp in seen or fp in exclude:
                continue
            seen.add(fp)
            unique.append(c)
        for c in unique:
            c.efficacy = round(await self._efficacy(c), 4)
            hyp_conf = next((h.confidence for h in hyps if h.category == c.addresses), d.selected.confidence)
            c.confidence = round(c.efficacy * hyp_conf, 4)
            await self._dry_run(c, d)
            if c.policy_allowed:
                c.utility = round(c.confidence * RISK_FACTOR.get(c.risk_level or "HIGH", 0.4)
                                  * (APPROVAL_FACTOR if c.requires_approval else 1.0), 4)
            else:
                c.utility = -1.0
        unique.sort(key=lambda c: c.utility, reverse=True)
        plan.candidates = unique
        if unique and unique[0].utility > 0:
            plan.selected_index = 0
        elif d.selected.category == CauseCategory.NETWORK_DEGRADATION:
            plan.escalation_reason = ("network path degradation has no safe automated remediation in the application "
                                      "layer; escalating to the network/platform owner")
        elif d.selected.category == CauseCategory.BAD_DEPLOYMENT and any(
                c.get("status", {}).get("phase") == "Aborted" and c["spec"]["targetRef"] == d.selected.component
                for c in b.canaries):
            plan.escalation_reason = "monitor: the canary was already aborted by progressive delivery"
        elif not unique and cands:
            plan.escalation_reason = (f"every candidate action for {d.selected.category.value} on {d.selected.component} "
                                      "was already attempted or denied in this incident")
        elif not unique:
            plan.escalation_reason = f"no playbook action for {d.selected.category.value}"
        else:
            plan.escalation_reason = "all candidate actions were denied by policy: " + "; ".join(
                f"{c.action_type.value}: {', '.join(c.policy_reasons[:2])}" for c in unique[:3])
        return plan

    async def _dry_run(self, c: RemediationCandidate, d: Diagnosis) -> None:
        req = {"incidentId": d.incident_id, "actionType": c.action_type.value, "target": c.target.model_dump(),
               "parameters": c.params.wire(), "diagnosisConfidence": int(round(d.confidence * 100))}
        try:
            res = await self.cp.evaluate(req)
        except (UpstreamUnavailable, ControlPlaneError) as exc:
            # Fail closed: without a policy verdict the candidate cannot be selected.
            c.policy_allowed, c.policy_reasons = False, [f"policy evaluation unavailable: {exc}"]
            return
        dec = res.get("decision", {})
        c.policy_allowed = bool(dec.get("allowed"))
        c.requires_approval = bool(dec.get("requiresApproval"))
        c.risk_level = dec.get("riskLevel")
        c.risk_score = dec.get("riskScore")
        c.policy_reasons = list(dec.get("reasons") or [])

