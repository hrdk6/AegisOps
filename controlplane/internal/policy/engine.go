package policy

import (
	"fmt"
	"math"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/hashing"
	"github.com/aegisops/aegisops/controlplane/internal/registry"
)

// evaluation accumulates checks while the engine runs.
type evaluation struct {
	checks           []v1.PolicyCheck
	denyReasons      []string
	approvalReasons  []string
	riskAdjustments  []string
	score            int32
	breakerOpen      bool
	simImproved      bool
	disruptionExceed bool
}

func (e *evaluation) pass(name, detail string) {
	e.checks = append(e.checks, v1.PolicyCheck{Name: name, Passed: true, Detail: detail})
}

func (e *evaluation) deny(name, detail string) {
	e.checks = append(e.checks, v1.PolicyCheck{Name: name, Passed: false, Detail: detail})
	e.denyReasons = append(e.denyReasons, name+": "+detail)
}

func (e *evaluation) needApproval(name, detail string) {
	e.checks = append(e.checks, v1.PolicyCheck{Name: name, Passed: true, Detail: "approval required: " + detail})
	e.approvalReasons = append(e.approvalReasons, name+": "+detail)
}

func (e *evaluation) addRisk(points int32, why string) {
	e.score += points
	e.riskAdjustments = append(e.riskAdjustments, fmt.Sprintf("%+d %s", points, why))
}

// ParamsHash identifies an action's type, target and parameters.
func ParamsHash(spec v1.RemediationActionSpec) string {
	return hashing.ProposalHash(spec.Target, spec.ActionType, spec.Parameters)
}

// Evaluate runs every deterministic rule and returns the decision.
func Evaluate(in Input) v1.PolicyDecision {
	e := &evaluation{}
	spec := in.Spec
	pol := in.Policy
	decision := func() v1.PolicyDecision { return finalize(in, e) }

	regSpec, known := registry.Lookup(spec.ActionType)
	switch {
	case !known:
		e.deny("registry", fmt.Sprintf("action type %q is not in the action registry", spec.ActionType))
		e.score = 100
		return decision()
	case regSpec.Prohibited:
		e.deny("registry", fmt.Sprintf("action class %q is prohibited by the AegisOps safety model", spec.ActionType))
		e.score = 100
		return decision()
	default:
		e.pass("registry", "allowlisted action")
	}
	e.score = regSpec.BaseRisk

	if err := registry.ValidateStructure(spec.ActionType, spec.Target, spec.Parameters, spec.RevertOf); err != nil {
		e.deny("structure", err.Error())
		return decision()
	}
	e.pass("structure", "parameters match action schema")

	if !containsStr(pol.AllowedNamespaces, spec.Target.Namespace) {
		e.deny("namespace-allowlist", fmt.Sprintf("namespace %q is outside the automation scope", spec.Target.Namespace))
		return decision()
	}
	e.pass("namespace-allowlist", spec.Target.Namespace)

	switch pol.Mode {
	case v1.ModeObserve:
		e.deny("mode", "policy is in observe mode: execution disabled")
	case v1.ModeSupervised:
		e.needApproval("mode", "policy is in supervised mode")
	default:
		e.pass("mode", pol.Mode)
	}

	rule := findRule(pol.ActionRules, spec.ActionType)
	if rule != nil && rule.Enabled != nil && !*rule.Enabled {
		e.deny("action-rule", fmt.Sprintf("action %s is disabled by policy", spec.ActionType))
	}
	if rule != nil && rule.RequireApproval {
		e.needApproval("action-rule", fmt.Sprintf("policy requires approval for %s", spec.ActionType))
	}

	if !in.Target.Exists {
		e.deny("target-exists", fmt.Sprintf("%s %s/%s not found", spec.Target.Kind, spec.Target.Namespace, spec.Target.Name))
		return decision()
	}
	e.pass("target-exists", in.Target.Deployment)

	checkProtected(in, e)
	checkPreconditions(in, e)
	checkPrerequisites(in, regSpec, e)
	if !in.ExecutionRecheck {
		checkBudgets(in, e)
	}
	checkInFlight(in, e)
	checkCircuitBreaker(in, e)
	checkSimulation(in, regSpec, e)
	scoreContext(in, regSpec, e)

	return decision()
}

func finalize(in Input, e *evaluation) v1.PolicyDecision {
	score := e.score
	if score < 0 {
		score = 0
	}
	if score > 100 {
		score = 100
	}
	level := levelFor(score)
	if rule := findRule(in.Policy.ActionRules, in.Spec.ActionType); rule != nil && rule.MinRisk.Rank() > level.Rank() {
		level = rule.MinRisk
	}

	allowed := len(e.denyReasons) == 0
	requiresApproval := len(e.approvalReasons) > 0
	if allowed {
		requiresApproval = requiresApproval || needsApprovalForRisk(in, e, level)
	}
	reasons := append([]string{}, e.denyReasons...)
	if allowed {
		reasons = append(reasons, e.approvalReasons...)
		if requiresApproval && len(e.approvalReasons) == 0 {
			reasons = append(reasons, fmt.Sprintf("risk %s meets approval threshold %s", level, in.Policy.ApprovalRiskThreshold))
		}
	}
	checks := append([]v1.PolicyCheck{}, e.checks...)
	checks = append(checks, v1.PolicyCheck{Name: "risk-score", Passed: true,
		Detail: fmt.Sprintf("score=%d level=%s [%s]", score, level, strings.Join(e.riskAdjustments, ", "))})

	return v1.PolicyDecision{
		Allowed:          allowed,
		RequiresApproval: allowed && requiresApproval,
		RiskLevel:        level,
		RiskScore:        score,
		Reasons:          reasons,
		Checks:           checks,
		Mode:             in.Policy.Mode,
		PolicyGeneration: in.PolicyGeneration,
		EvaluatedAt:      metav1.NewTime(in.Now),
	}
}

func needsApprovalForRisk(in Input, e *evaluation, level v1.RiskLevel) bool {
	if in.Spec.ActionType == v1.ActionRevert {
		// Restoring a known-good snapshot moves the system back toward a
		// previously observed state; it is permitted autonomously (budgeted).
		return level.Rank() >= v1.RiskHigh.Rank()
	}
	if level.Rank() >= v1.RiskHigh.Rank() {
		return true
	}
	threshold := in.Policy.ApprovalRiskThreshold
	if threshold == "" {
		threshold = v1.RiskMedium
	}
	if level.Rank() < threshold.Rank() {
		return false
	}
	// Simulation-gated autonomy: MEDIUM risk with an Improved sandbox verdict.
	if level == v1.RiskMedium && in.Policy.AllowSimulationAutoApproval && e.simImproved &&
		!e.breakerOpen && in.Spec.DiagnosisConfidence >= in.Policy.AutonomyMinConfidence {
		e.checks = append(e.checks, v1.PolicyCheck{Name: "simulation-gate", Passed: true,
			Detail: "MEDIUM risk auto-approved: sandbox simulation verdict Improved"})
		return false
	}
	return true
}

func levelFor(score int32) v1.RiskLevel {
	switch {
	case score < 30:
		return v1.RiskLow
	case score < 55:
		return v1.RiskMedium
	case score < 80:
		return v1.RiskHigh
	default:
		return v1.RiskCritical
	}
}

func checkProtected(in Input, e *evaluation) {
	for _, p := range in.Policy.ProtectedWorkloads {
		if p.Namespace == in.Target.Namespace && p.Name == in.Target.Deployment {
			if p.Mode == "approval" {
				e.needApproval("protected-workload", fmt.Sprintf("%s is protected (approval)", p.Name))
			} else {
				e.deny("protected-workload", fmt.Sprintf("%s is protected: automation may not modify it", p.Name))
			}
			return
		}
	}
	e.pass("protected-workload", "not protected")
}

// checkPreconditions rejects proposals planned against a target state that no
// longer holds (stale plans), e.g. a rollback computed before someone else rolled out.
func checkPreconditions(in Input, e *evaluation) {
	pre := in.Spec.Preconditions
	if pre == nil || pre.ExpectedRevision == nil {
		return
	}
	if in.Target.Kind != "Deployment" && in.Target.Kind != "Pod" {
		e.deny("preconditions", fmt.Sprintf("expectedRevision is not applicable to %s targets", in.Target.Kind))
		return
	}
	if in.Target.CurrentRevision != *pre.ExpectedRevision {
		e.deny("preconditions", fmt.Sprintf("stale plan: %s is at revision %d but the proposal was planned against revision %d",
			in.Target.Deployment, in.Target.CurrentRevision, *pre.ExpectedRevision))
		return
	}
	e.pass("preconditions", fmt.Sprintf("target still at planned revision %d", *pre.ExpectedRevision))
}

func checkPrerequisites(in Input, reg registry.Spec, e *evaluation) {
	spec := in.Spec
	t := in.Target
	lim := in.Policy.Limits
	switch spec.ActionType {
	case v1.ActionScaleDeployment:
		want := *spec.Parameters.Replicas
		switch {
		case want == t.Replicas:
			e.deny("prerequisites", fmt.Sprintf("deployment already has %d replicas (no-op)", want))
		case want < lim.MinReplicas:
			e.deny("limits", fmt.Sprintf("replicas %d below policy minimum %d", want, lim.MinReplicas))
		case want > lim.MaxReplicas:
			e.deny("limits", fmt.Sprintf("replicas %d above policy maximum %d", want, lim.MaxReplicas))
		case t.Replicas > 0 && want > t.Replicas && float64(want)/float64(t.Replicas) > lim.MaxScaleUpFactor:
			e.deny("limits", fmt.Sprintf("scale-up factor %.2f exceeds policy maximum %.2f",
				float64(want)/float64(t.Replicas), lim.MaxScaleUpFactor))
		default:
			e.pass("limits", fmt.Sprintf("%d -> %d replicas within [%d,%d]", t.Replicas, want, lim.MinReplicas, lim.MaxReplicas))
		}
	case v1.ActionRollbackDeployment:
		target := int64(0)
		if spec.Parameters.ToRevision != nil {
			target = *spec.Parameters.ToRevision
			if target == t.CurrentRevision {
				e.deny("prerequisites", fmt.Sprintf("revision %d is already current", target))
				return
			}
			if !containsInt(t.Revisions, target) {
				e.deny("prerequisites", fmt.Sprintf("revision %d not found in rollout history", target))
				return
			}
		} else {
			for _, r := range t.Revisions {
				if r < t.CurrentRevision && r > target {
					target = r
				}
			}
			if target == 0 {
				e.deny("prerequisites", "no previous revision available for rollback")
				return
			}
		}
		e.pass("prerequisites", fmt.Sprintf("rollback %d -> %d", t.CurrentRevision, target))
	case v1.ActionPatchResources:
		c := spec.Parameters.Container
		if c == "" && len(t.Containers) != 1 {
			e.deny("prerequisites", "container must be specified for multi-container workloads")
			return
		}
		if c != "" && !containsStr(t.Containers, c) {
			e.deny("prerequisites", fmt.Sprintf("container %q not found", c))
			return
		}
		r := spec.Parameters.Resources
		if exceeds(r.CPULimit, lim.MaxCPU) || exceeds(r.CPURequest, lim.MaxCPU) {
			e.deny("limits", fmt.Sprintf("cpu exceeds policy maximum %s", lim.MaxCPU))
			return
		}
		if exceeds(r.MemoryLimit, lim.MaxMemory) || exceeds(r.MemoryRequest, lim.MaxMemory) {
			e.deny("limits", fmt.Sprintf("memory exceeds policy maximum %s", lim.MaxMemory))
			return
		}
		e.pass("limits", "resources within policy maximums")
	case v1.ActionPauseRollout:
		if t.Kind == "CanaryRelease" {
			if t.CanaryPhase != v1.CanaryProgressing {
				e.deny("prerequisites", fmt.Sprintf("canary is %s, not Progressing", t.CanaryPhase))
				return
			}
		} else if t.Paused {
			e.deny("prerequisites", "rollout already paused")
			return
		}
		e.pass("prerequisites", "rollout can be paused")
	case v1.ActionResumeRollout:
		if t.Kind == "CanaryRelease" && t.CanaryPhase != v1.CanaryPaused {
			e.deny("prerequisites", fmt.Sprintf("canary is %s, not Paused", t.CanaryPhase))
			return
		}
		if t.Kind == "Deployment" && !t.Paused {
			e.deny("prerequisites", "rollout is not paused")
			return
		}
		e.pass("prerequisites", "rollout can be resumed")
	case v1.ActionUpdateConfig:
		if !containsStr(t.ConfigMaps, spec.Parameters.ConfigMap) {
			e.deny("prerequisites", fmt.Sprintf("configmap %q is not consumed by %s", spec.Parameters.ConfigMap, t.Deployment))
			return
		}
		if !t.ConfigRevisionAvailable {
			e.deny("prerequisites", "no recorded configmap revision available to restore")
			return
		}
		e.pass("prerequisites", "recorded configmap revision available")
	case v1.ActionAbortCanary:
		if t.CanaryPhase != v1.CanaryProgressing && t.CanaryPhase != v1.CanaryPaused && t.CanaryPhase != v1.CanaryPending {
			e.deny("prerequisites", fmt.Sprintf("canary is %s; nothing to abort", t.CanaryPhase))
			return
		}
		e.pass("prerequisites", "canary in progress")
	case v1.ActionRevert:
		rt := in.RevertTarget
		switch {
		case rt == nil:
			e.deny("prerequisites", fmt.Sprintf("action %q to revert not found", spec.RevertOf))
		case rt.IncidentID != spec.IncidentID:
			e.deny("prerequisites", "revert must belong to the same incident as the original action")
		case rt.Phase != v1.PhaseSucceeded && rt.Phase != v1.PhaseFailed:
			e.deny("prerequisites", fmt.Sprintf("action %s is %s; only executed actions can be reverted", rt.Name, rt.Phase))
		case !rt.Reversible:
			e.deny("prerequisites", fmt.Sprintf("action %s has no reversible snapshot", rt.Name))
		case rt.RevertedBy != "":
			e.deny("prerequisites", fmt.Sprintf("action %s already reverted by %s", rt.Name, rt.RevertedBy))
		default:
			e.pass("prerequisites", "reversible snapshot present")
		}
	case v1.ActionRestartPod, v1.ActionIsolatePod:
		if t.Deployment == "" {
			e.deny("prerequisites", "pod is not owned by a Deployment")
			return
		}
		e.pass("prerequisites", "pod owned by "+t.Deployment)
	default:
		e.pass("prerequisites", "none")
	}

	if reg.Disruptive && t.Replicas > 0 {
		pct := int32(100)
		if spec.ActionType == v1.ActionRestartPod {
			pct = int32(math.Ceil(100.0 / float64(t.Replicas)))
		}
		if spec.ActionType == v1.ActionRestartPod && pct > in.Policy.Limits.MaxDisruptionPercent {
			e.disruptionExceed = true
			e.needApproval("disruption", fmt.Sprintf("restarting 1 of %d pods disrupts %d%% (> %d%%)", t.Replicas, pct, in.Policy.Limits.MaxDisruptionPercent))
		}
	}
}

func checkBudgets(in Input, e *evaluation) {
	b := in.Policy.Budgets
	spec := in.Spec
	now := in.Now
	myHash := ParamsHash(spec)

	incidentActions, incidentReverts, globalRecent := 0, 0, 0
	var lastOnTarget *time.Time
	for _, h := range in.History {
		if h.Name == in.Name {
			continue
		}
		admitted := h.Phase != v1.PhaseDenied && h.Phase != v1.PhaseRejected && h.Phase != v1.PhaseExpired
		if !admitted {
			continue
		}
		if h.IncidentID == spec.IncidentID {
			if h.Type == v1.ActionRevert {
				incidentReverts++
			} else {
				incidentActions++
			}
			if h.Type == spec.ActionType && h.ParamsHash == myHash && spec.ActionType != v1.ActionRevert {
				e.deny("deduplication", fmt.Sprintf("identical action %s already %s for this incident; it will not be retried", h.Name, h.Phase))
			}
		}
		if h.Type != v1.ActionRevert && now.Sub(h.CreatedAt) < time.Duration(b.GlobalWindowSeconds)*time.Second {
			globalRecent++
		}
		if h.TargetKey == spec.Target.Key() {
			ts := h.CreatedAt
			if h.CompletedAt != nil {
				ts = *h.CompletedAt
			}
			if lastOnTarget == nil || ts.After(*lastOnTarget) {
				t := ts
				lastOnTarget = &t
			}
		}
	}

	if spec.ActionType == v1.ActionRevert {
		if int32(incidentReverts) >= b.MaxRevertsPerIncident {
			e.deny("revert-budget", fmt.Sprintf("incident already used %d/%d reverts", incidentReverts, b.MaxRevertsPerIncident))
		} else {
			e.pass("revert-budget", fmt.Sprintf("%d/%d reverts used", incidentReverts, b.MaxRevertsPerIncident))
		}
		return // reverts bypass cooldown and action budgets
	}

	if int32(incidentActions) >= b.MaxActionsPerIncident {
		e.deny("incident-budget", fmt.Sprintf("incident already used %d/%d actions; escalate to a human", incidentActions, b.MaxActionsPerIncident))
	} else {
		e.pass("incident-budget", fmt.Sprintf("%d/%d actions used", incidentActions, b.MaxActionsPerIncident))
	}
	if int32(globalRecent) >= b.GlobalMaxActionsPerWindow {
		e.deny("global-rate-limit", fmt.Sprintf("%d actions in the last %ds (max %d)", globalRecent, b.GlobalWindowSeconds, b.GlobalMaxActionsPerWindow))
	} else {
		e.pass("global-rate-limit", fmt.Sprintf("%d/%d in window", globalRecent, b.GlobalMaxActionsPerWindow))
	}
	if lastOnTarget != nil {
		elapsed := now.Sub(*lastOnTarget)
		cooldown := time.Duration(b.TargetCooldownSeconds) * time.Second
		if elapsed < cooldown {
			e.deny("target-cooldown", fmt.Sprintf("target changed %ds ago; cooldown is %ds", int(elapsed.Seconds()), b.TargetCooldownSeconds))
			return
		}
	}
	e.pass("target-cooldown", "no recent action on target")
}

func checkInFlight(in Input, e *evaluation) {
	for _, h := range in.History {
		if h.Name == in.Name {
			continue
		}
		inflight := h.Phase == v1.PhasePending || h.Phase == v1.PhaseApproved || h.Phase == v1.PhaseExecuting
		awaiting := h.Phase == v1.PhaseAwaitingApproval
		sameIncident := h.IncidentID == in.Spec.IncidentID
		sameTarget := h.TargetKey == in.Spec.Target.Key()
		if (inflight && (sameIncident || sameTarget)) || (awaiting && sameIncident && !in.ExecutionRecheck) {
			e.deny("in-flight-lock", fmt.Sprintf("action %s (%s) is %s; one action at a time per incident and target", h.Name, h.Type, h.Phase))
			return
		}
	}
	e.pass("in-flight-lock", "no conflicting action in flight")
}

// BreakerState derives the circuit-breaker state from recent action outcomes.
func BreakerState(history []ActionRecord, cb v1.CircuitBreakerSpec, now time.Time) (open bool, failures int32, last *time.Time) {
	window := time.Duration(cb.WindowSeconds) * time.Second
	for _, h := range history {
		if h.Phase != v1.PhaseFailed && h.Phase != v1.PhaseRolledBack {
			continue
		}
		ts := h.CreatedAt
		if h.CompletedAt != nil {
			ts = *h.CompletedAt
		}
		if now.Sub(ts) > window {
			continue
		}
		failures++
		if last == nil || ts.After(*last) {
			t := ts
			last = &t
		}
	}
	open = cb.FailureThreshold > 0 && failures >= cb.FailureThreshold && last != nil &&
		now.Sub(*last) < time.Duration(cb.OpenSeconds)*time.Second
	return open, failures, last
}

func checkCircuitBreaker(in Input, e *evaluation) {
	open, failures, _ := BreakerState(in.History, in.Policy.Budgets.CircuitBreaker, in.Now)
	if open && in.Spec.ActionType != v1.ActionRevert {
		e.breakerOpen = true
		e.needApproval("circuit-breaker", fmt.Sprintf("breaker open after %d failed interventions; autonomy suspended", failures))
		e.addRisk(10, "circuit breaker open")
		return
	}
	e.pass("circuit-breaker", fmt.Sprintf("closed (%d recent failures)", failures))
}

func checkSimulation(in Input, reg registry.Spec, e *evaluation) {
	if in.Spec.SimulationRef == "" {
		if reg.Simulatable {
			e.addRisk(5, "no sandbox simulation")
		}
		return
	}
	s := in.Simulation
	if s == nil {
		e.deny("simulation", fmt.Sprintf("referenced simulation %q not found", in.Spec.SimulationRef))
		return
	}
	if s.ProposalHash != ParamsHash(in.Spec) {
		e.deny("simulation", "simulation does not match this action's target and parameters")
		return
	}
	if s.Phase != v1.SimCompleted {
		e.pass("simulation", fmt.Sprintf("simulation %s (%s): policy-only evaluation", s.Phase, s.Name))
		return
	}
	switch s.Verdict {
	case v1.VerdictImproved:
		e.simImproved = true
		e.addRisk(-5, "sandbox verdict Improved")
		e.pass("simulation", "sandbox verdict Improved")
	case v1.VerdictRegressed:
		e.deny("simulation", "sandbox verdict Regressed: proposal made the service worse")
	default:
		e.pass("simulation", "sandbox verdict "+s.Verdict)
	}
}

func scoreContext(in Input, reg registry.Spec, e *evaluation) {
	t := in.Target
	switch t.Tier {
	case TierCritical:
		e.addRisk(10, "critical tier")
	case TierStateful:
		e.addRisk(20, "stateful tier")
	case TierInfrastructure:
		e.addRisk(25, "infrastructure tier")
	}
	if n := int32(len(t.Dependents)); n > 0 {
		pts := 3 * n
		if pts > 9 {
			pts = 9
		}
		e.addRisk(pts, fmt.Sprintf("%d dependent services", n))
	}
	if in.Spec.ActionType == v1.ActionScaleDeployment && in.Spec.Parameters.Replicas != nil {
		want := *in.Spec.Parameters.Replicas
		switch {
		case want < t.Replicas:
			e.addRisk(15, "scale down reduces capacity")
		case t.Replicas == 0:
			e.addRisk(5, "scale from zero")
		case float64(want)/float64(t.Replicas) > 2:
			e.addRisk(10, "large scale-up")
		}
	}
	if e.disruptionExceed {
		e.addRisk(20, "disruption above limit")
	}
	c := in.Spec.DiagnosisConfidence
	switch {
	case in.Spec.ActionType == v1.ActionRevert:
	case c == 0:
		e.addRisk(10, "no diagnosis confidence supplied")
	case c < 50:
		e.addRisk(15, "low diagnosis confidence")
		e.needApproval("confidence", fmt.Sprintf("diagnosis confidence %d%% below autonomy minimum %d%%", c, in.Policy.AutonomyMinConfidence))
	case c < in.Policy.AutonomyMinConfidence:
		e.addRisk(5, "moderate diagnosis confidence")
		e.needApproval("confidence", fmt.Sprintf("diagnosis confidence %d%% below autonomy minimum %d%%", c, in.Policy.AutonomyMinConfidence))
	}
	_ = reg
}

func findRule(rules []v1.ActionRule, t v1.ActionType) *v1.ActionRule {
	for i := range rules {
		if rules[i].ActionType == t {
			return &rules[i]
		}
	}
	return nil
}

func exceeds(val, max string) bool {
	if val == "" || max == "" {
		return false
	}
	v, err1 := resource.ParseQuantity(val)
	m, err2 := resource.ParseQuantity(max)
	if err1 != nil || err2 != nil {
		return true // fail closed on unparsable values
	}
	return v.Cmp(m) > 0
}

func containsStr(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func containsInt(list []int64, v int64) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
