package controllers

import (
	"context"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/go-logr/logr"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/actions"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

const sysNS = "aegis-system"

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := v1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

func testPolicy() *v1.AegisPolicy {
	spec := v1.DefaultPolicySpec()
	spec.Mode = v1.ModeAutonomous
	spec.AllowedNamespaces = []string{"shop"}
	spec.AllowSimulationAutoApproval = true
	spec.Budgets.TargetCooldownSeconds = 0
	spec.ProtectedWorkloads = []v1.ProtectedWorkload{{Namespace: "shop", Name: "postgres", Mode: "deny"}}
	return &v1.AegisPolicy{ObjectMeta: metav1.ObjectMeta{Name: "default"}, Spec: spec}
}

func ptr[T any](v T) *T { return &v }

// testDeployment builds a Deployment with rollout history (revisions 1..current).
func testDeployment(name, tier string, replicas int32, revisions int) []client.Object {
	uid := types.UID("uid-" + name)
	d := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: name, UID: uid,
			Annotations: map[string]string{state.AnnotationTier: tier, state.AnnotationRevision: itoa(revisions)}},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr(replicas),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": name, "track": "stable"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": name, "track": "stable", "version": "v" + itoa(revisions)}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "app", Image: "shopflow:" + itoa(revisions)}}},
			},
		},
		Status: appsv1.DeploymentStatus{Replicas: replicas, UpdatedReplicas: replicas, ReadyReplicas: replicas, AvailableReplicas: replicas},
	}
	objs := []client.Object{d}
	for i := 1; i <= revisions; i++ {
		rs := &appsv1.ReplicaSet{
			ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: name + "-rs" + itoa(i),
				Labels:          map[string]string{"pod-template-hash": "h" + itoa(i)},
				Annotations:     map[string]string{state.AnnotationRevision: itoa(i)},
				OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: name, UID: uid, Controller: ptr(true)}}},
			Spec: appsv1.ReplicaSetSpec{Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": name, "track": "stable", "version": "v" + itoa(i), "pod-template-hash": "h" + itoa(i)}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "app", Image: "shopflow:" + itoa(i)}}},
			}},
		}
		objs = append(objs, rs)
	}
	return objs
}

func itoa(i int) string { return string(rune('0' + i)) }

type harness struct {
	t      *testing.T
	client client.Client
	rec    *ActionReconciler
	now    time.Time
}

func newHarness(t *testing.T, objs ...client.Object) *harness {
	t.Helper()
	objs = append(objs, testPolicy())
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(objs...).
		WithStatusSubresource(&v1.RemediationAction{}, &v1.AegisPolicy{}, &appsv1.Deployment{}).Build()
	h := &harness{t: t, client: c, now: time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)}
	hist := history.NewStore(c, sysNS, 10)
	res := &state.Resolver{Client: c, History: hist}
	clock := func() time.Time { return h.now }
	h.rec = &ActionReconciler{
		Client: c, Recorder: record.NewFakeRecorder(100), Policy: &PolicyProvider{Client: c, Name: "default"},
		Resolver: res, Audit: audit.New(logr.Discard(), 100), Now: clock,
		Executor: &actions.Executor{Client: c, Resolver: res, History: hist, Now: clock},
	}
	return h
}

func (h *harness) create(name string, spec v1.RemediationActionSpec) {
	h.t.Helper()
	spec.RequestedBy = "test"
	if spec.IncidentID == "" {
		spec.IncidentID = "INC-1"
	}
	a := &v1.RemediationAction{ObjectMeta: metav1.ObjectMeta{Namespace: sysNS, Name: name,
		CreationTimestamp: metav1.NewTime(h.now)}, Spec: spec}
	if err := h.client.Create(context.Background(), a); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) reconcile(name string) ctrl.Result {
	h.t.Helper()
	res, err := h.rec.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: sysNS, Name: name}})
	if err != nil {
		h.t.Fatalf("reconcile %s: %v", name, err)
	}
	return res
}

func (h *harness) action(name string) *v1.RemediationAction {
	h.t.Helper()
	var a v1.RemediationAction
	if err := h.client.Get(context.Background(), client.ObjectKey{Namespace: sysNS, Name: name}, &a); err != nil {
		h.t.Fatal(err)
	}
	return &a
}

func (h *harness) deployment(name string) *appsv1.Deployment {
	h.t.Helper()
	var d appsv1.Deployment
	if err := h.client.Get(context.Background(), client.ObjectKey{Namespace: "shop", Name: name}, &d); err != nil {
		h.t.Fatal(err)
	}
	return &d
}

// settle simulates the Deployment controller finishing a rollout.
func (h *harness) settle(name string) {
	h.t.Helper()
	d := h.deployment(name)
	r := *d.Spec.Replicas
	d.Status = appsv1.DeploymentStatus{ObservedGeneration: d.Generation, Replicas: r, UpdatedReplicas: r, ReadyReplicas: r, AvailableReplicas: r}
	if err := h.client.Status().Update(context.Background(), d); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) expectPhase(name string, want v1.ActionPhase) *v1.RemediationAction {
	h.t.Helper()
	a := h.action(name)
	if a.Status.Phase != want {
		h.t.Fatalf("%s phase = %s, want %s (message: %s)", name, a.Status.Phase, want, a.Status.Message)
	}
	return a
}
