package actions

import (
	"context"
	"errors"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/state"
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

func testClient(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := v1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return fake.NewClientBuilder().WithScheme(s).WithObjects(objs...).Build()
}

func crashingPod(name, hash string, created time.Time) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: name, CreationTimestamp: metav1.NewTime(created),
			Labels: map[string]string{"app": "inventory", "pod-template-hash": hash}},
		Status: corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{{Name: "app", RestartCount: 5,
			State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "CrashLoopBackOff"}}}}},
	}
}

// Regression (found in the live benchmark): restoring resources re-activated an *older*
// ReplicaSet; the fast-fail check picked the most recent ReplicaSet (the faulty one) and failed
// the action on its crashing pods in the same second it started.
func TestCrashLoopCheckIgnoresFaultyOldReplicaSet(t *testing.T) {
	start := time.Date(2026, 9, 29, 1, 49, 55, 0, time.UTC)
	uid := types.UID("uid-inv")
	owner := []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: "inventory", UID: uid, Controller: ptr(true)}}
	rs := func(name, hash, rev string) *appsv1.ReplicaSet {
		return &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: name, OwnerReferences: owner,
			Labels: map[string]string{"pod-template-hash": hash}, Annotations: map[string]string{"deployment.kubernetes.io/revision": rev}}}
	}
	d := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: "inventory", UID: uid, Generation: 9,
			Annotations: map[string]string{"deployment.kubernetes.io/revision": "7"}},
		Spec: appsv1.DeploymentSpec{Replicas: ptr(int32(1)),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "inventory"}}},
		Status: appsv1.DeploymentStatus{ObservedGeneration: 9, Replicas: 2, UpdatedReplicas: 1},
	}
	// Revision 6 is the faulty low-memory template; the fix re-activated the old golden ReplicaSet as revision 7.
	c := testClient(t, d, rs("inv-bad", "bad", "6"), rs("inv-golden", "golden", "7"),
		crashingPod("inv-bad-1", "bad", start.Add(-time.Minute)))
	x := &Executor{Client: c, Resolver: &state.Resolver{Client: c}}
	prog, err := x.checkDeployment(context.Background(), "shop", "inventory", start)
	if err != nil || prog.Failed {
		t.Fatalf("faulty pre-existing pods must not fail the fix: %+v %v", prog, err)
	}
	// A pod of the *current* revision created by this rollout that crash-loops does fail it.
	if err := c.Create(context.Background(), crashingPod("inv-golden-1", "golden", start.Add(10*time.Second))); err != nil {
		t.Fatal(err)
	}
	if prog, _ := x.checkDeployment(context.Background(), "shop", "inventory", start); !prog.Failed {
		t.Fatalf("a crash-looping pod of the new revision must fail the action: %+v", prog)
	}
}

func TestRevertOfAlreadyUnhealthyStateIsReportedAsRestored(t *testing.T) {
	snap, err := encode(SnapDeployment, true, deploymentSnapshot{Name: "inventory", Replicas: 2, ReadyReplicas: 0}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if !wasUnhealthy(snap) {
		t.Fatal("0/2 ready at snapshot time is unhealthy")
	}
	healthy, _ := encode(SnapDeployment, true, deploymentSnapshot{Name: "inventory", Replicas: 2, ReadyReplicas: 2}, time.Now())
	if wasUnhealthy(healthy) {
		t.Fatal("2/2 ready is healthy")
	}
	cfg, _ := encode(SnapConfig, true, configSnapshot{Deployment: deploymentSnapshot{Name: "auth", Replicas: 2, ReadyReplicas: 1}}, time.Now())
	if !wasUnhealthy(cfg) {
		t.Fatal("config snapshots carry the deployment's health")
	}
	legacy := &v1.Snapshot{Kind: SnapDeployment, Data: `{"name":"inventory","replicas":2}`}
	if wasUnhealthy(legacy) {
		t.Fatal("snapshots without recorded readiness must not be assumed unhealthy")
	}
}

func ptr[T any](v T) *T { return &v }
