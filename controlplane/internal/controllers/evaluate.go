package controllers

import (
	"context"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

// Evaluator assembles policy inputs from cluster state. It is shared by the
// reconciler (authoritative decisions) and the API (dry-run decisions) so that
// both always evaluate identically.
type Evaluator struct {
	Client          client.Reader
	Policy          *PolicyProvider
	Resolver        *state.Resolver
	SystemNamespace string
}

// Input builds the policy input for an action (persisted or transient).
func (e *Evaluator) Input(ctx context.Context, a *v1.RemediationAction, recheck bool, now time.Time) (policy.Input, map[string]*v1.RemediationAction, error) {
	pol, gen, err := e.Policy.Get(ctx)
	if err != nil {
		return policy.Input{}, nil, err
	}
	hist, byName, err := ActionHistory(ctx, e.Client, e.SystemNamespace)
	if err != nil {
		return policy.Input{}, nil, err
	}
	ts, err := e.Resolver.Target(ctx, a.Spec.Target, a.Spec.Parameters.ConfigMap, a.Spec.Parameters.ConfigRevision)
	if err != nil {
		return policy.Input{}, nil, err
	}
	in := policy.Input{Name: a.Name, Spec: a.Spec, Policy: pol, PolicyGeneration: gen, Target: ts, History: hist,
		Now: now, ExecutionRecheck: recheck}
	if a.Spec.SimulationRef != "" {
		var sim v1.RemediationSimulation
		if err := e.Client.Get(ctx, client.ObjectKey{Namespace: e.SystemNamespace, Name: a.Spec.SimulationRef}, &sim); err == nil {
			in.Simulation = &policy.SimulationState{Name: sim.Name, Phase: sim.Status.Phase, Verdict: sim.Status.Verdict,
				ProposalHash: sim.Status.ProposalHash}
		} else if !apierrors.IsNotFound(err) {
			return policy.Input{}, nil, err
		}
	}
	if a.Spec.RevertOf != "" {
		if orig, ok := byName[a.Spec.RevertOf]; ok {
			rec := Record(orig)
			in.RevertTarget = &rec
		}
	}
	return in, byName, nil
}
