package actions

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/history"
)

// Regression (found in the live benchmark): update_config resolved "previous" at apply time.
// The restore itself appends to the history, so a re-apply by the reconciler resolved
// "previous" to the faulty data and re-broke the service 36ms after fixing it.
func TestUpdateConfigReapplyIsIdempotent(t *testing.T) {
	ctx := context.Background()
	good := map[string]string{"AUTH_CONFIG": `{"issuer": "shopflow"}`}
	bad := map[string]string{"AUTH_CONFIG": `{"issuer": `}
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: "auth-config"}, Data: bad}
	c := testClient(t, cm)
	store := history.NewStore(c, "aegis-system", 10)
	record := func(data map[string]string) {
		if err := store.Record(ctx, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: "shop", Name: "auth-config"}, Data: data}); err != nil {
			t.Fatal(err)
		}
	}
	record(good)
	record(bad) // the faulty push
	x := &Executor{Client: c, History: store}
	a := &v1.RemediationAction{ObjectMeta: metav1.ObjectMeta{Name: "act-1"}, Spec: v1.RemediationActionSpec{
		ActionType: v1.ActionUpdateConfig, Target: v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: "auth"},
		Parameters: v1.ActionParameters{ConfigMap: "auth-config", ConfigRevision: "previous"}}}
	target, err := store.Resolve(ctx, "shop", "auth-config", "previous")
	if err != nil {
		t.Fatal(err)
	}
	snap, err := encode(SnapConfig, true, configSnapshot{ConfigMap: "auth-config", Data: bad, RestoreRevision: target.Revision}, x.now())
	if err != nil {
		t.Fatal(err)
	}
	a.Status.Snapshot = snap
	for i := 0; i < 2; i++ { // the second pass is the reconciler's crash-safe re-apply
		_, _ = x.Apply(ctx, a, nil) // the Deployment restart fails here (no Deployment); the ConfigMap write is what matters
		var live corev1.ConfigMap
		if err := c.Get(ctx, client.ObjectKey{Namespace: "shop", Name: "auth-config"}, &live); err != nil {
			t.Fatal(err)
		}
		if live.Data["AUTH_CONFIG"] != good["AUTH_CONFIG"] {
			t.Fatalf("apply %d restored %q, want the good revision", i+1, live.Data["AUTH_CONFIG"])
		}
		record(live.Data) // what the change recorder does on the ConfigMap update event
	}
}
