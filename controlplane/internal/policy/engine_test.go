package policy

import (
	"strings"
	"testing"
	"time"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

var now = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

func i32(v int32) *int32 { return &v }
func i64(v int64) *int64 { return &v }

func basePolicy() v1.AegisPolicySpec {
	p := v1.DefaultPolicySpec()
	p.Mode = v1.ModeAutonomous
	p.AllowedNamespaces = []string{"shop"}
	p.AllowSimulationAutoApproval = true
	p.Budgets.MaxActionsPerIncident = 3
	p.Budgets.MaxRevertsPerIncident = 1
	p.Budgets.TargetCooldownSeconds = 120
	p.Budgets.GlobalMaxActionsPerWindow = 10
	p.Budgets.CircuitBreaker = v1.CircuitBreakerSpec{FailureThreshold: 2, WindowSeconds: 900, OpenSeconds: 600}
	p.ProtectedWorkloads = []v1.ProtectedWorkload{{Namespace: "shop", Name: "postgres", Mode: "deny"}}
	return p
}

func deployTarget(name string) TargetState {
	return TargetState{
		Exists: true, Kind: "Deployment", Namespace: "shop", Name: name, Deployment: name,
		Replicas: 2, Ready: 2, Tier: TierStandard, Revisions: []int64{1, 2, 3}, CurrentRevision: 3,
		Containers: []string{"app"}, ConfigMaps: []string{name + "-config"}, ConfigRevisionAvailable: true,
	}
}

func spec(t v1.ActionType, name string, p v1.ActionParameters) v1.RemediationActionSpec {
	return v1.RemediationActionSpec{
		IncidentID: "INC-1", ActionType: t, Target: v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: name},
		Parameters: p, DiagnosisConfidence: 85, RequestedBy: "test",
	}
}

func eval(s v1.RemediationActionSpec, target TargetState, mut ...func(*Input)) v1.PolicyDecision {
	in := Input{Spec: s, Policy: basePolicy(), Target: target, Now: now}
	for _, m := range mut {
		m(&in)
	}
	return Evaluate(in)
}

func hasCheckFailed(d v1.PolicyDecision, name string) bool {
	for _, c := range d.Checks {
		if c.Name == name && !c.Passed {
			return true
		}
	}
	return false
}

func TestProhibitedActionsAlwaysDenied(t *testing.T) {
	for _, a := range []v1.ActionType{v1.ActionExecCommand, v1.ActionDeleteVolume, v1.ActionModifyRBAC, v1.ActionDeleteWorkload, v1.ActionSchemaMigration} {
		d := eval(spec(a, "payment", v1.ActionParameters{}), deployTarget("payment"))
		if d.Allowed {
			t.Fatalf("%s must be denied", a)
		}
		if d.RiskLevel != v1.RiskCritical {
			t.Fatalf("%s risk = %s, want CRITICAL", a, d.RiskLevel)
		}
	}
}

func TestUnknownActionDenied(t *testing.T) {
	d := eval(spec("kubectl_apply", "payment", v1.ActionParameters{}), deployTarget("payment"))
	if d.Allowed || !hasCheckFailed(d, "registry") {
		t.Fatalf("unknown action must fail registry check: %+v", d)
	}
}

func TestLowRiskScaleIsAutonomous(t *testing.T) {
	d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(4)}), deployTarget("payment"))
	if !d.Allowed || d.RequiresApproval || d.RiskLevel != v1.RiskLow {
		t.Fatalf("expected autonomous LOW scale, got %+v", d)
	}
}

func TestScaleLimits(t *testing.T) {
	cases := map[string]int32{"to zero": 0, "above max": 9, "factor too large": 5, "no-op": 2}
	for name, r := range cases {
		d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(r)}), deployTarget("payment"))
		if d.Allowed {
			t.Errorf("%s: expected deny, got allowed", name)
		}
	}
}

func TestNamespaceOutsideScopeDenied(t *testing.T) {
	s := spec(v1.ActionScaleDeployment, "coredns", v1.ActionParameters{Replicas: i32(3)})
	s.Target.Namespace = "kube-system"
	d := eval(s, deployTarget("coredns"))
	if d.Allowed || !hasCheckFailed(d, "namespace-allowlist") {
		t.Fatalf("expected namespace denial: %+v", d)
	}
}

func TestProtectedWorkloadDenied(t *testing.T) {
	d := eval(spec(v1.ActionRolloutRestart, "postgres", v1.ActionParameters{}), deployTarget("postgres"))
	if d.Allowed || !hasCheckFailed(d, "protected-workload") {
		t.Fatalf("expected protected-workload denial: %+v", d)
	}
}

func TestObserveModeDeniesEverything(t *testing.T) {
	d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)}), deployTarget("payment"),
		func(in *Input) { in.Policy.Mode = v1.ModeObserve })
	if d.Allowed {
		t.Fatal("observe mode must deny execution")
	}
}

func TestSupervisedModeRequiresApproval(t *testing.T) {
	d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)}), deployTarget("payment"),
		func(in *Input) { in.Policy.Mode = v1.ModeSupervised })
	if !d.Allowed || !d.RequiresApproval {
		t.Fatalf("supervised mode must require approval: %+v", d)
	}
}

func TestRollbackMediumNeedsApprovalWithoutSimulation(t *testing.T) {
	target := deployTarget("payment")
	target.Tier = TierCritical
	d := eval(spec(v1.ActionRollbackDeployment, "payment", v1.ActionParameters{}), target)
	if !d.Allowed || !d.RequiresApproval || d.RiskLevel != v1.RiskMedium {
		t.Fatalf("expected MEDIUM + approval, got %+v", d)
	}
}

func TestSimulationGatedAutonomy(t *testing.T) {
	target := deployTarget("payment")
	target.Tier = TierCritical
	s := spec(v1.ActionRollbackDeployment, "payment", v1.ActionParameters{})
	s.SimulationRef = "sim-1"
	sim := &SimulationState{Name: "sim-1", Phase: v1.SimCompleted, Verdict: v1.VerdictImproved, ProposalHash: ParamsHash(s)}
	d := eval(s, target, func(in *Input) { in.Simulation = sim })
	if !d.Allowed || d.RequiresApproval {
		t.Fatalf("improved simulation should auto-approve MEDIUM: %+v", d)
	}

	// A simulation of a *different* proposal must not be accepted.
	sim.ProposalHash = "deadbeef"
	d = eval(s, target, func(in *Input) { in.Simulation = sim })
	if d.Allowed {
		t.Fatalf("mismatched simulation must be denied: %+v", d)
	}

	// A regressed simulation blocks the action.
	sim.ProposalHash = ParamsHash(s)
	sim.Verdict = v1.VerdictRegressed
	d = eval(s, target, func(in *Input) { in.Simulation = sim })
	if d.Allowed {
		t.Fatal("regressed simulation must deny")
	}
}

func TestLowConfidenceRequiresApproval(t *testing.T) {
	s := spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)})
	s.DiagnosisConfidence = 40
	d := eval(s, deployTarget("payment"))
	if !d.Allowed || !d.RequiresApproval {
		t.Fatalf("low confidence must require approval: %+v", d)
	}
}

func TestHighConfidenceClaimCannotBypassHighRisk(t *testing.T) {
	s := spec(v1.ActionApplyNetworkPolicy, "payment", v1.ActionParameters{NetworkPolicyTemplate: "deny-all-ingress"})
	s.DiagnosisConfidence = 100
	d := eval(s, deployTarget("payment"))
	if !d.RequiresApproval {
		t.Fatalf("HIGH risk must always require approval: %+v", d)
	}
}

func TestIncidentBudget(t *testing.T) {
	var hist []ActionRecord
	for i, typ := range []v1.ActionType{v1.ActionRolloutRestart, v1.ActionScaleDeployment, v1.ActionPatchResources} {
		hist = append(hist, ActionRecord{Name: string(rune('a' + i)), IncidentID: "INC-1", Type: typ,
			TargetKey: "Deployment/shop/other", Phase: v1.PhaseFailed, CreatedAt: now.Add(-time.Hour)})
	}
	d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)}), deployTarget("payment"),
		func(in *Input) { in.History = hist })
	if d.Allowed || !hasCheckFailed(d, "incident-budget") {
		t.Fatalf("expected incident budget denial: %+v", d)
	}
}

func TestFailedActionIsNotRetried(t *testing.T) {
	s := spec(v1.ActionRolloutRestart, "payment", v1.ActionParameters{})
	hist := []ActionRecord{{Name: "prev", IncidentID: "INC-1", Type: v1.ActionRolloutRestart, TargetKey: s.Target.Key(),
		ParamsHash: ParamsHash(s), Phase: v1.PhaseFailed, CreatedAt: now.Add(-time.Hour)}}
	d := eval(s, deployTarget("payment"), func(in *Input) { in.History = hist })
	if d.Allowed || !hasCheckFailed(d, "deduplication") {
		t.Fatalf("expected dedup denial: %+v", d)
	}
}

func TestTargetCooldown(t *testing.T) {
	s := spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)})
	done := now.Add(-30 * time.Second)
	hist := []ActionRecord{{Name: "prev", IncidentID: "INC-0", Type: v1.ActionRolloutRestart, TargetKey: s.Target.Key(),
		Phase: v1.PhaseSucceeded, CreatedAt: now.Add(-time.Minute), CompletedAt: &done}}
	d := eval(s, deployTarget("payment"), func(in *Input) { in.History = hist })
	if d.Allowed || !hasCheckFailed(d, "target-cooldown") {
		t.Fatalf("expected cooldown denial: %+v", d)
	}
}

func TestInFlightLock(t *testing.T) {
	s := spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)})
	hist := []ActionRecord{{Name: "running", IncidentID: "INC-1", Type: v1.ActionRollbackDeployment,
		TargetKey: "Deployment/shop/order", Phase: v1.PhaseExecuting, CreatedAt: now.Add(-10 * time.Minute)}}
	d := eval(s, deployTarget("payment"), func(in *Input) { in.History = hist })
	if d.Allowed || !hasCheckFailed(d, "in-flight-lock") {
		t.Fatalf("expected in-flight denial: %+v", d)
	}
}

func TestCircuitBreakerEscalatesToHuman(t *testing.T) {
	var hist []ActionRecord
	for i := 0; i < 2; i++ {
		done := now.Add(-time.Duration(5+i) * time.Minute)
		hist = append(hist, ActionRecord{Name: "f" + string(rune('0'+i)), IncidentID: "INC-9", Type: v1.ActionRolloutRestart,
			TargetKey: "Deployment/shop/x" + string(rune('0'+i)), Phase: v1.PhaseRolledBack,
			CreatedAt: done.Add(-time.Minute), CompletedAt: &done})
	}
	d := eval(spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)}), deployTarget("payment"),
		func(in *Input) { in.History = hist })
	if !d.Allowed || !d.RequiresApproval {
		t.Fatalf("open breaker must force approval: %+v", d)
	}
	open, failures, _ := BreakerState(hist, basePolicy().Budgets.CircuitBreaker, now)
	if !open || failures != 2 {
		t.Fatalf("breaker open=%v failures=%d", open, failures)
	}
}

func TestRevertBudgetAndPrereqs(t *testing.T) {
	s := spec(v1.ActionRevert, "payment", v1.ActionParameters{})
	s.RevertOf = "act-1"
	orig := &ActionRecord{Name: "act-1", IncidentID: "INC-1", Type: v1.ActionRollbackDeployment, Phase: v1.PhaseSucceeded, Reversible: true}
	d := eval(s, deployTarget("payment"), func(in *Input) { in.RevertTarget = orig })
	if !d.Allowed || d.RequiresApproval {
		t.Fatalf("revert of reversible action should be autonomous: %+v", d)
	}
	hist := []ActionRecord{{Name: "rv-0", IncidentID: "INC-1", Type: v1.ActionRevert, Phase: v1.PhaseSucceeded, CreatedAt: now}}
	d = eval(s, deployTarget("payment"), func(in *Input) { in.RevertTarget = orig; in.History = hist })
	if d.Allowed || !hasCheckFailed(d, "revert-budget") {
		t.Fatalf("expected revert budget denial: %+v", d)
	}
	orig.Reversible = false
	d = eval(s, deployTarget("payment"), func(in *Input) { in.RevertTarget = orig })
	if d.Allowed {
		t.Fatal("non-reversible action cannot be reverted")
	}
}

func TestResourceLimitsEnforced(t *testing.T) {
	d := eval(spec(v1.ActionPatchResources, "payment", v1.ActionParameters{
		Resources: &v1.ResourcePatch{MemoryLimit: "64Gi"}}), deployTarget("payment"))
	if d.Allowed {
		t.Fatal("memory above policy max must be denied")
	}
}

func TestRestartOnlyPodNeedsApproval(t *testing.T) {
	s := spec(v1.ActionRestartPod, "payment-abc", v1.ActionParameters{})
	s.Target.Kind = "Pod"
	target := deployTarget("payment")
	target.Kind = "Pod"
	target.Replicas = 1
	d := eval(s, target)
	if !d.RequiresApproval {
		t.Fatalf("restarting 100%% of pods must require approval: %+v", d)
	}
}

func TestUpdateConfigRequiresConsumedConfigMap(t *testing.T) {
	d := eval(spec(v1.ActionUpdateConfig, "auth", v1.ActionParameters{ConfigMap: "other-config"}), deployTarget("auth"))
	if d.Allowed || !strings.Contains(strings.Join(d.Reasons, " "), "not consumed") {
		t.Fatalf("expected configmap consumption denial: %+v", d)
	}
}

func TestExecutionRecheckSkipsAdmissionBudgets(t *testing.T) {
	s := spec(v1.ActionScaleDeployment, "payment", v1.ActionParameters{Replicas: i32(3)})
	var hist []ActionRecord
	for i := 0; i < 5; i++ {
		hist = append(hist, ActionRecord{Name: "x" + string(rune('0'+i)), IncidentID: "INC-1", Type: v1.ActionPauseRollout,
			TargetKey: "Deployment/shop/y", Phase: v1.PhaseSucceeded, CreatedAt: now.Add(-time.Hour)})
	}
	d := eval(s, deployTarget("payment"), func(in *Input) { in.History = hist; in.ExecutionRecheck = true; in.Name = "self" })
	if !d.Allowed {
		t.Fatalf("recheck must not re-apply admission budgets: %+v", d)
	}
}

func TestRollbackPrerequisites(t *testing.T) {
	target := deployTarget("payment")
	target.Revisions = []int64{3}
	d := eval(spec(v1.ActionRollbackDeployment, "payment", v1.ActionParameters{}), target)
	if d.Allowed {
		t.Fatal("rollback without history must be denied")
	}
	d = eval(spec(v1.ActionRollbackDeployment, "payment", v1.ActionParameters{ToRevision: i64(7)}), deployTarget("payment"))
	if d.Allowed {
		t.Fatal("rollback to unknown revision must be denied")
	}
}

func TestStalePlanDenied(t *testing.T) {
	// Planned against revision 3 (the bad release); someone rolled out revision 4
	// in the meantime, so "rollback to previous" would now target the bad release.
	s := spec(v1.ActionRollbackDeployment, "payment", v1.ActionParameters{ToRevision: i64(2)})
	s.Preconditions = &v1.ActionPreconditions{ExpectedRevision: i64(3)}
	target := deployTarget("payment")
	target.Revisions, target.CurrentRevision = []int64{2, 3, 4}, 4
	d := eval(s, target)
	if d.Allowed || !hasCheckFailed(d, "preconditions") {
		t.Fatalf("stale plan must be denied: %+v", d)
	}
	for _, recheck := range []bool{false, true} {
		target.CurrentRevision = 3
		d = eval(s, target, func(in *Input) { in.ExecutionRecheck = recheck })
		if hasCheckFailed(d, "preconditions") {
			t.Fatalf("matching revision must pass (recheck=%v): %+v", recheck, d.Checks)
		}
	}
}
