package controllers

import (
	"context"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

// PolicyStatusReporter periodically publishes derived policy state (circuit
// breaker) on the AegisPolicy status and as a metric. It runs as a
// leader-elected manager Runnable.
type PolicyStatusReporter struct {
	Client          client.Client
	PolicyName      string
	SystemNamespace string
	Interval        time.Duration
}

// Start implements manager.Runnable.
func (p *PolicyStatusReporter) Start(ctx context.Context) error {
	t := time.NewTicker(p.Interval)
	defer t.Stop()
	for {
		if err := p.report(ctx); err != nil {
			log.FromContext(ctx).Error(err, "policy status report failed")
		}
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
		}
	}
}

func (p *PolicyStatusReporter) report(ctx context.Context) error {
	var pol v1.AegisPolicy
	if err := p.Client.Get(ctx, client.ObjectKey{Name: p.PolicyName}, &pol); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return err
	}
	hist, _, err := ActionHistory(ctx, p.Client, p.SystemNamespace)
	if err != nil {
		return err
	}
	open, failures, last := policy.BreakerState(hist, pol.Spec.Budgets.CircuitBreaker, time.Now())
	cb := v1.CircuitBreakerStatus{State: "closed", RecentFailures: failures}
	telemetry.BreakerOpen.Set(0)
	if open {
		cb.State = "open"
		cb.Reason = "too many failed or rolled-back interventions; autonomous execution requires approval"
		t := metav1.NewTime(*last)
		cb.OpenedAt = &t
		telemetry.BreakerOpen.Set(1)
	}
	if pol.Status.CircuitBreaker.State == cb.State && pol.Status.CircuitBreaker.RecentFailures == cb.RecentFailures &&
		pol.Status.ObservedGeneration == pol.Generation {
		return nil
	}
	pol.Status.CircuitBreaker = cb
	pol.Status.ObservedGeneration = pol.Generation
	now := metav1.Now()
	pol.Status.LastUpdated = &now
	return p.Client.Status().Update(ctx, &pol)
}
