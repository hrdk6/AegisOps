package controllers

import (
	"context"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/go-logr/logr"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/hashing"
	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/sandbox"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

func simHarness(t *testing.T, objs ...client.Object) (client.Client, *SimulationReconciler) {
	t.Helper()
	objs = append(objs, testPolicy())
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(objs...).
		WithStatusSubresource(&v1.RemediationSimulation{}).Build()
	hist := history.NewStore(c, sysNS, 10)
	return c, &SimulationReconciler{
		Client: c, Recorder: record.NewFakeRecorder(50), Policy: &PolicyProvider{Client: c, Name: "default"},
		Resolver: &state.Resolver{Client: c, History: hist}, History: hist, Audit: audit.New(logr.Discard(), 50),
		Sandbox: sandbox.Config{Namespace: "aegis-sandbox", PostgresImage: "pg", RedisImage: "redis", ProbeImage: "probe",
			ServiceAccount: "runner"},
		Now: func() time.Time { return time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC) },
	}
}

func TestSimulationPersistsProposalHashForPolicyBinding(t *testing.T) {
	objs := testDeployment("payment", "critical", 2, 2)
	d := objs[0].(*appsv1.Deployment)
	d.Annotations[state.AnnotationSimulationProfile] = "postgres"
	d.Annotations[state.AnnotationSimulationProbe] = `{"path":"/charge","method":"POST"}`
	target := v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: "payment"}
	sim := &v1.RemediationSimulation{ObjectMeta: metav1.ObjectMeta{Namespace: sysNS, Name: "sim-1"},
		Spec: v1.RemediationSimulationSpec{IncidentID: "INC-1", Target: target, RequestedBy: "test",
			Proposal: v1.SimulationProposal{ActionType: v1.ActionRollbackDeployment},
			Load:     v1.LoadProfile{RPS: 10, DurationSeconds: 10, Concurrency: 2}}}
	c, r := simHarness(t, append(objs, sim)...)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: sysNS, Name: "sim-1"}}); err != nil {
		t.Fatal(err)
	}
	var got v1.RemediationSimulation
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: sysNS, Name: "sim-1"}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != v1.SimProvisioning {
		t.Fatalf("phase = %s (%s)", got.Status.Phase, got.Status.Reason)
	}
	want := hashing.ProposalHash(target, v1.ActionRollbackDeployment, v1.ActionParameters{})
	if got.Status.ProposalHash != want {
		t.Fatalf("proposalHash %q, want %q (must match the action's ParamsHash)", got.Status.ProposalHash, want)
	}
	var deps appsv1.DeploymentList
	_ = c.List(context.Background(), &deps, client.InNamespace("aegis-sandbox"))
	if len(deps.Items) != 2 {
		t.Fatalf("expected baseline and candidate clones, got %d", len(deps.Items))
	}
	for _, dep := range deps.Items {
		if dep.Spec.Template.Spec.ServiceAccountName != "runner" {
			t.Fatal("clone must run as the sandbox identity")
		}
	}
}

func TestSimulationUnavailableWithoutOptIn(t *testing.T) {
	objs := testDeployment("payment", "critical", 2, 2)
	sim := &v1.RemediationSimulation{ObjectMeta: metav1.ObjectMeta{Namespace: sysNS, Name: "sim-2"},
		Spec: v1.RemediationSimulationSpec{IncidentID: "INC-1", RequestedBy: "test",
			Target:   v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: "payment"},
			Proposal: v1.SimulationProposal{ActionType: v1.ActionScaleDeployment}}}
	c, r := simHarness(t, append(objs, sim)...)
	_, _ = r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: sysNS, Name: "sim-2"}})
	var got v1.RemediationSimulation
	_ = c.Get(context.Background(), client.ObjectKey{Namespace: sysNS, Name: "sim-2"}, &got)
	if got.Status.Phase != v1.SimUnavailable {
		t.Fatalf("scale is not simulatable; phase = %s", got.Status.Phase)
	}
}
