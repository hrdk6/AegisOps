package controllers

import (
	"testing"
	"time"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

func dep(name string) v1.TargetRef {
	return v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: name}
}

func TestAutonomousScaleLifecycleAndRevert(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "standard", 2, 2)...)
	h.create("scale-1", v1.RemediationActionSpec{ActionType: v1.ActionScaleDeployment, Target: dep("payment"),
		Parameters: v1.ActionParameters{Replicas: ptr(int32(4))}, DiagnosisConfidence: 90})

	h.reconcile("scale-1") // admission
	a := h.expectPhase("scale-1", v1.PhaseApproved)
	if a.Status.Decision == nil || a.Status.Decision.RiskLevel != v1.RiskLow || a.Status.SpecHash == "" {
		t.Fatalf("unexpected decision: %+v", a.Status.Decision)
	}

	h.reconcile("scale-1") // snapshot + apply
	a = h.expectPhase("scale-1", v1.PhaseExecuting)
	if !a.Status.Applied || a.Status.Snapshot == nil || !a.Status.Snapshot.Reversible {
		t.Fatalf("expected applied reversible snapshot, got %+v", a.Status)
	}
	if got := *h.deployment("payment").Spec.Replicas; got != 4 {
		t.Fatalf("replicas = %d, want 4", got)
	}

	h.settle("payment")
	h.reconcile("scale-1")
	h.expectPhase("scale-1", v1.PhaseSucceeded)

	// Revert restores the pre-action snapshot and marks the original RolledBack.
	h.create("scale-1-revert", v1.RemediationActionSpec{ActionType: v1.ActionRevert, Target: dep("payment"), RevertOf: "scale-1"})
	h.reconcile("scale-1-revert")
	h.expectPhase("scale-1-revert", v1.PhaseApproved)
	h.reconcile("scale-1-revert")
	h.expectPhase("scale-1-revert", v1.PhaseExecuting)
	if got := *h.deployment("payment").Spec.Replicas; got != 2 {
		t.Fatalf("replicas after revert = %d, want 2", got)
	}
	h.settle("payment")
	h.reconcile("scale-1-revert")
	h.expectPhase("scale-1-revert", v1.PhaseSucceeded)
	orig := h.expectPhase("scale-1", v1.PhaseRolledBack)
	if orig.Status.RevertedBy != "scale-1-revert" {
		t.Fatalf("revertedBy = %q", orig.Status.RevertedBy)
	}
}

func TestProhibitedActionDenied(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "standard", 2, 1)...)
	h.create("exec-1", v1.RemediationActionSpec{ActionType: v1.ActionExecCommand, Target: dep("payment"), DiagnosisConfidence: 99})
	h.reconcile("exec-1")
	a := h.expectPhase("exec-1", v1.PhaseDenied)
	if a.Status.CompletedAt == nil {
		t.Fatal("denied action must be terminal")
	}
	// Further reconciles never execute a denied action.
	h.reconcile("exec-1")
	h.expectPhase("exec-1", v1.PhaseDenied)
}

func TestRollbackRequiresApprovalThenExpires(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "critical", 2, 3)...)
	h.create("rb-1", v1.RemediationActionSpec{ActionType: v1.ActionRollbackDeployment, Target: dep("payment"), DiagnosisConfidence: 85})
	res := h.reconcile("rb-1")
	h.expectPhase("rb-1", v1.PhaseAwaitingApproval)
	if res.RequeueAfter <= 0 {
		t.Fatal("awaiting approval must requeue for expiry")
	}
	if img := h.deployment("payment").Spec.Template.Spec.Containers[0].Image; img != "shopflow:3" {
		t.Fatalf("deployment mutated before approval: %s", img)
	}
	h.now = h.now.Add(time.Hour)
	h.reconcile("rb-1")
	h.expectPhase("rb-1", v1.PhaseExpired)
}

func TestApprovedRollbackRestoresPreviousTemplate(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "critical", 2, 3)...)
	h.create("rb-2", v1.RemediationActionSpec{ActionType: v1.ActionRollbackDeployment, Target: dep("payment"), DiagnosisConfidence: 85})
	h.reconcile("rb-2")
	a := h.expectPhase("rb-2", v1.PhaseAwaitingApproval)

	// Simulate a verified human approval (the API writes this after HMAC verification).
	a.Status.Phase = v1.PhaseApproved
	a.Status.Approval = &v1.ApprovalRecord{Decision: "approved", Approver: "alice", SignatureVerified: true}
	if err := h.client.Status().Update(t.Context(), a); err != nil {
		t.Fatal(err)
	}
	h.reconcile("rb-2")
	h.expectPhase("rb-2", v1.PhaseExecuting)
	d := h.deployment("payment")
	if img := d.Spec.Template.Spec.Containers[0].Image; img != "shopflow:2" {
		t.Fatalf("rollback image = %s, want shopflow:2", img)
	}
	if _, ok := d.Spec.Template.Labels["pod-template-hash"]; ok {
		t.Fatal("pod-template-hash must be stripped from restored template")
	}
}

func TestUnapprovedActionCannotSkipToExecution(t *testing.T) {
	// An action that requires approval but is forced to Approved without a
	// verified approval record is sent back to AwaitingApproval on re-check.
	h := newHarness(t, testDeployment("payment", "critical", 2, 3)...)
	h.create("rb-3", v1.RemediationActionSpec{ActionType: v1.ActionRollbackDeployment, Target: dep("payment"), DiagnosisConfidence: 85})
	h.reconcile("rb-3")
	a := h.expectPhase("rb-3", v1.PhaseAwaitingApproval)
	a.Status.Phase = v1.PhaseApproved // forged transition without Approval record
	if err := h.client.Status().Update(t.Context(), a); err != nil {
		t.Fatal(err)
	}
	h.reconcile("rb-3")
	h.expectPhase("rb-3", v1.PhaseAwaitingApproval)
	if img := h.deployment("payment").Spec.Template.Spec.Containers[0].Image; img != "shopflow:3" {
		t.Fatalf("deployment mutated without approval: %s", img)
	}
}

func TestExecutionTimeoutTriggersAutoRevert(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "standard", 2, 2)...)
	h.create("scale-2", v1.RemediationActionSpec{ActionType: v1.ActionScaleDeployment, Target: dep("payment"),
		Parameters: v1.ActionParameters{Replicas: ptr(int32(3))}, DiagnosisConfidence: 90})
	h.reconcile("scale-2")
	h.reconcile("scale-2")
	h.expectPhase("scale-2", v1.PhaseExecuting)
	// Rollout never completes (status not settled); advance past the timeout.
	h.now = h.now.Add(10 * time.Minute)
	h.reconcile("scale-2")
	h.expectPhase("scale-2", v1.PhaseFailed)
	rv := h.action("scale-2-revert")
	if rv.Spec.ActionType != v1.ActionRevert || rv.Spec.RevertOf != "scale-2" {
		t.Fatalf("expected auto-revert request, got %+v", rv.Spec)
	}
}

func TestIncidentBudgetStopsRunawayLoop(t *testing.T) {
	h := newHarness(t, testDeployment("payment", "standard", 1, 1)...)
	// Budget is 3 actions per incident in the default policy.
	for i, r := range []int32{2, 3, 4, 5} {
		name := "loop-" + itoa(i)
		h.create(name, v1.RemediationActionSpec{ActionType: v1.ActionScaleDeployment, Target: dep("payment"),
			Parameters: v1.ActionParameters{Replicas: ptr(r)}, DiagnosisConfidence: 90})
		h.reconcile(name)
		a := h.action(name)
		if i < 3 {
			if a.Status.Phase == v1.PhaseDenied && a.Status.Decision != nil {
				for _, c := range a.Status.Decision.Checks {
					if c.Name == "incident-budget" && !c.Passed {
						t.Fatalf("budget exhausted too early at %d", i)
					}
				}
			}
			// Drive to completion so the next action is not blocked by the in-flight lock.
			h.reconcile(name)
			h.settle("payment")
			h.reconcile(name)
			continue
		}
		if a.Status.Phase != v1.PhaseDenied {
			t.Fatalf("4th action should be denied by incident budget, got %s: %s", a.Status.Phase, a.Status.Message)
		}
	}
}
