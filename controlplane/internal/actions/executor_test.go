package actions

import (
	"context"
	"errors"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

func deployAt(rev string, lastAction string) *appsv1.Deployment {
	ann := map[string]string{"deployment.kubernetes.io/revision": rev}
	if lastAction != "" {
		ann["aegisops.io/last-action"] = lastAction
	}
	return &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: "payment", Annotations: ann}}
}

func TestExpectationGuardsStalePlans(t *testing.T) {
	rev := int64(3)
	a := &v1.RemediationAction{ObjectMeta: metav1.ObjectMeta{Name: "act-1"},
		Spec: v1.RemediationActionSpec{Preconditions: &v1.ActionPreconditions{ExpectedRevision: &rev}}}
	ctx := withExpectation(context.Background(), a)

	if err := checkExpectation(ctx, deployAt("3", "")); err != nil {
		t.Fatalf("matching revision rejected: %v", err)
	}
	if err := checkExpectation(ctx, deployAt("4", "")); !errors.Is(err, ErrStalePlan) {
		t.Fatalf("changed revision must be a stale plan, got %v", err)
	}
	// Crash-safe re-apply: the revision moved because this action already wrote it.
	if err := checkExpectation(ctx, deployAt("4", "act-1")); err != nil {
		t.Fatalf("re-apply of own write rejected: %v", err)
	}
	// Actions without preconditions are unaffected.
	if err := checkExpectation(context.Background(), deployAt("9", "")); err != nil {
		t.Fatalf("no precondition must pass: %v", err)
	}
}
