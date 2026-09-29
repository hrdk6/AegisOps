package controllers

import (
	"context"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
)

// PolicyProvider loads the active AegisPolicy. When the policy object is
// missing it falls back to v1.DefaultPolicySpec(), which is supervised and has
// an empty namespace allowlist: nothing can execute (fail closed).
type PolicyProvider struct {
	Client client.Reader
	Name   string
}

// Get returns the active policy spec and its generation.
func (p *PolicyProvider) Get(ctx context.Context) (v1.AegisPolicySpec, int64, error) {
	var pol v1.AegisPolicy
	err := p.Client.Get(ctx, client.ObjectKey{Name: p.Name}, &pol)
	if apierrors.IsNotFound(err) {
		return v1.DefaultPolicySpec(), 0, nil
	}
	if err != nil {
		return v1.AegisPolicySpec{}, 0, err
	}
	return pol.Spec, pol.Generation, nil
}

// ActionHistory returns compact records of every RemediationAction in ns.
func ActionHistory(ctx context.Context, c client.Reader, ns string) ([]policy.ActionRecord, map[string]*v1.RemediationAction, error) {
	var list v1.RemediationActionList
	if err := c.List(ctx, &list, client.InNamespace(ns)); err != nil {
		return nil, nil, err
	}
	out := make([]policy.ActionRecord, 0, len(list.Items))
	byName := make(map[string]*v1.RemediationAction, len(list.Items))
	for i := range list.Items {
		a := &list.Items[i]
		byName[a.Name] = a
		out = append(out, Record(a))
	}
	return out, byName, nil
}

// Record converts an action to its policy record.
func Record(a *v1.RemediationAction) policy.ActionRecord {
	r := policy.ActionRecord{
		Name: a.Name, IncidentID: a.Spec.IncidentID, Type: a.Spec.ActionType, TargetKey: a.Spec.Target.Key(),
		ParamsHash: policy.ParamsHash(a.Spec), Phase: a.Status.Phase, CreatedAt: a.CreationTimestamp.Time,
		RevertOf: a.Spec.RevertOf, RevertedBy: a.Status.RevertedBy,
		Reversible: a.Status.Snapshot != nil && a.Status.Snapshot.Reversible,
	}
	if r.Phase == "" {
		r.Phase = v1.PhasePending
	}
	if a.Status.CompletedAt != nil {
		t := a.Status.CompletedAt.Time
		r.CompletedAt = &t
	}
	return r
}

func nowTime(f func() time.Time) metav1.Time {
	if f == nil {
		return metav1.Now()
	}
	return metav1.NewTime(f())
}
