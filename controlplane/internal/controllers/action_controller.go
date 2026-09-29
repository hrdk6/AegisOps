package controllers

import (
	"context"
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/actions"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/hashing"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
	"github.com/aegisops/aegisops/controlplane/internal/state"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

// AnnotationTraceParent carries the W3C trace context of the request that
// created an object so reconcile spans join the incident's trace.
const AnnotationTraceParent = "aegisops.io/traceparent"

// ControllerIdentity is the requester recorded for controller-initiated actions.
const ControllerIdentity = "system:aegisops-controller"

// ActionReconciler drives RemediationActions through admission, approval,
// execution and completion.
type ActionReconciler struct {
	client.Client
	Recorder record.EventRecorder
	Policy   *PolicyProvider
	Resolver *state.Resolver
	Executor *actions.Executor
	Audit    *audit.Log
	Now      func() time.Time
}

func (r *ActionReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// Reconcile implements reconcile.Reconciler.
func (r *ActionReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var a v1.RemediationAction
	if err := r.Get(ctx, req.NamespacedName, &a); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	ctx = propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{"traceparent": a.Annotations[AnnotationTraceParent]})
	ctx, span := telemetry.Tracer.Start(ctx, "RemediationAction.reconcile")
	defer span.End()
	span.SetAttributes(attribute.String("aegis.action", a.Name), attribute.String("aegis.action_type", string(a.Spec.ActionType)),
		attribute.String("aegis.incident_id", a.Spec.IncidentID), attribute.String("aegis.phase", string(a.Status.Phase)))

	var (
		res ctrl.Result
		err error
	)
	switch a.Status.Phase {
	case "", v1.PhasePending:
		res, err = r.admit(ctx, &a)
	case v1.PhaseAwaitingApproval:
		res, err = r.awaitApproval(ctx, &a)
	case v1.PhaseApproved:
		res, err = r.execute(ctx, &a)
	case v1.PhaseExecuting:
		res, err = r.progress(ctx, &a)
	}
	if apierrors.IsConflict(err) {
		return ctrl.Result{Requeue: true}, nil
	}
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	return res, err
}

func (r *ActionReconciler) buildInput(ctx context.Context, a *v1.RemediationAction, recheck bool) (policy.Input, map[string]*v1.RemediationAction, error) {
	ev := Evaluator{Client: r.Client, Policy: r.Policy, Resolver: r.Resolver, SystemNamespace: a.Namespace}
	return ev.Input(ctx, a, recheck, r.now())
}

func (r *ActionReconciler) setPhase(a *v1.RemediationAction, phase v1.ActionPhase, msg string) {
	a.Status.Phase = phase
	a.Status.Message = msg
	a.Status.ObservedGeneration = a.Generation
	if phase.IsTerminal() && a.Status.CompletedAt == nil {
		t := metav1.NewTime(r.now())
		a.Status.CompletedAt = &t
	}
	telemetry.ActionTransitions.WithLabelValues(string(a.Spec.ActionType), string(phase)).Inc()
}

func (r *ActionReconciler) admit(ctx context.Context, a *v1.RemediationAction) (ctrl.Result, error) {
	in, byName, err := r.buildInput(ctx, a, false)
	if err != nil {
		return ctrl.Result{}, err
	}
	d := policy.Evaluate(in)
	if a.Spec.ActionType == v1.ActionRevert {
		if orig, ok := byName[a.Spec.RevertOf]; ok && orig.Spec.Target != a.Spec.Target {
			d.Allowed = false
			d.RequiresApproval = false
			d.Reasons = append(d.Reasons, "revert target must equal the original action's target")
		}
	}
	a.Status.Decision = &d
	a.Status.SpecHash = hashing.SpecHash(a.Spec)
	outcome := "denied"
	switch {
	case !d.Allowed:
		r.setPhase(a, v1.PhaseDenied, "denied by policy: "+strings.Join(d.Reasons, "; "))
	case d.RequiresApproval:
		outcome = "approval_required"
		r.setPhase(a, v1.PhaseAwaitingApproval, "awaiting human approval: "+strings.Join(d.Reasons, "; "))
	default:
		outcome = "auto_approved"
		r.setPhase(a, v1.PhaseApproved, fmt.Sprintf("auto-approved by policy (risk %s, score %d)", d.RiskLevel, d.RiskScore))
	}
	if err := r.Status().Update(ctx, a); err != nil {
		return ctrl.Result{}, err
	}
	telemetry.PolicyDecisions.WithLabelValues(string(a.Spec.ActionType), outcome, string(d.RiskLevel)).Inc()
	r.Audit.Record(a.Spec.RequestedBy, "policy.evaluate", "remediationaction/"+a.Name, outcome, map[string]any{
		"incident": a.Spec.IncidentID, "actionType": a.Spec.ActionType, "target": a.Spec.Target.Key(),
		"risk": d.RiskLevel, "score": d.RiskScore, "reasons": d.Reasons})
	r.Recorder.Eventf(a, eventType(d.Allowed), "PolicyDecision", "%s (risk %s): %s", outcome, d.RiskLevel, strings.Join(d.Reasons, "; "))
	if a.Status.Phase == v1.PhaseAwaitingApproval {
		pol, _, _ := r.Policy.Get(ctx)
		return ctrl.Result{RequeueAfter: time.Duration(pol.ApprovalTTLSeconds) * time.Second}, nil
	}
	return ctrl.Result{}, nil
}

func (r *ActionReconciler) awaitApproval(ctx context.Context, a *v1.RemediationAction) (ctrl.Result, error) {
	pol, _, err := r.Policy.Get(ctx)
	if err != nil {
		return ctrl.Result{}, err
	}
	deadline := a.CreationTimestamp.Add(time.Duration(pol.ApprovalTTLSeconds) * time.Second)
	if r.now().After(deadline) {
		r.setPhase(a, v1.PhaseExpired, "approval window expired without a decision")
		if err := r.Status().Update(ctx, a); err != nil {
			return ctrl.Result{}, err
		}
		r.Audit.Record(ControllerIdentity, "approval.expire", "remediationaction/"+a.Name, "expired", nil)
		return ctrl.Result{}, nil
	}
	return ctrl.Result{RequeueAfter: deadline.Sub(r.now())}, nil
}

func (r *ActionReconciler) original(ctx context.Context, a *v1.RemediationAction) (*v1.RemediationAction, error) {
	if a.Spec.RevertOf == "" {
		return nil, nil
	}
	var orig v1.RemediationAction
	if err := r.Get(ctx, client.ObjectKey{Namespace: a.Namespace, Name: a.Spec.RevertOf}, &orig); err != nil {
		return nil, err
	}
	return &orig, nil
}

func (r *ActionReconciler) execute(ctx context.Context, a *v1.RemediationAction) (ctrl.Result, error) {
	// Time-of-use re-check: the world may have changed since admission.
	in, _, err := r.buildInput(ctx, a, true)
	if err != nil {
		return ctrl.Result{}, err
	}
	d := policy.Evaluate(in)
	humanApproved := a.Status.Approval != nil && a.Status.Approval.Decision == "approved" && a.Status.Approval.SignatureVerified
	if !d.Allowed {
		r.setPhase(a, v1.PhaseDenied, "execution-time policy re-check failed: "+strings.Join(d.Reasons, "; "))
		a.Status.Decision = &d
		if err := r.Status().Update(ctx, a); err != nil {
			return ctrl.Result{}, err
		}
		r.Audit.Record(ControllerIdentity, "policy.recheck", "remediationaction/"+a.Name, "denied", map[string]any{"reasons": d.Reasons})
		return ctrl.Result{}, nil
	}
	if d.RequiresApproval && !humanApproved {
		a.Status.Decision = &d
		r.setPhase(a, v1.PhaseAwaitingApproval, "execution-time re-check requires approval: "+strings.Join(d.Reasons, "; "))
		if err := r.Status().Update(ctx, a); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: time.Minute}, nil
	}
	if a.Status.SpecHash != hashing.SpecHash(a.Spec) {
		r.setPhase(a, v1.PhaseDenied, "spec hash mismatch: spec changed after admission")
		return ctrl.Result{}, r.Status().Update(ctx, a)
	}

	orig, err := r.original(ctx, a)
	if err != nil {
		return r.fail(ctx, a, "cannot load original action: "+err.Error(), false)
	}
	snap, err := r.Executor.Snapshot(ctx, a)
	if err != nil {
		return r.fail(ctx, a, "snapshot failed: "+err.Error(), false)
	}
	a.Status.Snapshot = snap
	a.Status.Applied = false
	start := metav1.NewTime(r.now())
	a.Status.StartedAt = &start
	r.setPhase(a, v1.PhaseExecuting, "snapshot taken; applying")
	// Persist the snapshot *before* mutating so a crash never loses it.
	if err := r.Status().Update(ctx, a); err != nil {
		return ctrl.Result{}, err
	}
	r.Audit.Record(ControllerIdentity, "action.execute", "remediationaction/"+a.Name, "started",
		map[string]any{"actionType": a.Spec.ActionType, "target": a.Spec.Target.Key(), "incident": a.Spec.IncidentID})
	return r.apply(ctx, a, orig)
}

func (r *ActionReconciler) apply(ctx context.Context, a *v1.RemediationAction, orig *v1.RemediationAction) (ctrl.Result, error) {
	ctx, span := telemetry.Tracer.Start(ctx, "RemediationAction.apply")
	defer span.End()
	msg, err := r.Executor.Apply(ctx, a, orig)
	if err != nil {
		span.RecordError(err)
		return r.fail(ctx, a, "apply failed: "+err.Error(), false)
	}
	a.Status.Applied = true
	a.Status.Message = msg
	if err := r.Status().Update(ctx, a); err != nil {
		return ctrl.Result{}, err
	}
	r.Recorder.Event(a, corev1.EventTypeNormal, "Applied", msg)
	log.FromContext(ctx).Info("action applied", "action", a.Name, "type", a.Spec.ActionType, "message", msg)
	return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
}

func (r *ActionReconciler) progress(ctx context.Context, a *v1.RemediationAction) (ctrl.Result, error) {
	orig, err := r.original(ctx, a)
	if err != nil {
		return r.fail(ctx, a, "cannot load original action: "+err.Error(), false)
	}
	if !a.Status.Applied {
		return r.apply(ctx, a, orig)
	}
	pol, _, err := r.Policy.Get(ctx)
	if err != nil {
		return ctrl.Result{}, err
	}
	timeout := time.Duration(pol.ExecutionTimeoutSeconds) * time.Second
	if a.Status.StartedAt != nil && r.now().Sub(a.Status.StartedAt.Time) > timeout {
		return r.fail(ctx, a, fmt.Sprintf("execution timed out after %s", timeout), true)
	}
	p, err := r.Executor.Check(ctx, a, orig)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return r.fail(ctx, a, "target disappeared during execution: "+err.Error(), false)
		}
		return ctrl.Result{}, err
	}
	switch {
	case p.Failed:
		return r.fail(ctx, a, p.Message, true)
	case p.Done:
		r.setPhase(a, v1.PhaseSucceeded, p.Message)
		if err := r.Status().Update(ctx, a); err != nil {
			return ctrl.Result{}, err
		}
		telemetry.ActionDuration.WithLabelValues(string(a.Spec.ActionType), "succeeded").Observe(r.now().Sub(a.Status.StartedAt.Time).Seconds())
		r.Audit.Record(ControllerIdentity, "action.complete", "remediationaction/"+a.Name, "succeeded", map[string]any{"message": p.Message})
		r.Recorder.Event(a, corev1.EventTypeNormal, "Succeeded", p.Message)
		if orig != nil {
			return ctrl.Result{}, r.markRolledBack(ctx, orig, a.Name)
		}
		return ctrl.Result{}, nil
	default:
		if a.Status.Message != p.Message {
			a.Status.Message = p.Message
			if err := r.Status().Update(ctx, a); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
	}
}

func (r *ActionReconciler) fail(ctx context.Context, a *v1.RemediationAction, msg string, mayRevert bool) (ctrl.Result, error) {
	r.setPhase(a, v1.PhaseFailed, msg)
	if err := r.Status().Update(ctx, a); err != nil {
		return ctrl.Result{}, err
	}
	if a.Status.StartedAt != nil {
		telemetry.ActionDuration.WithLabelValues(string(a.Spec.ActionType), "failed").Observe(r.now().Sub(a.Status.StartedAt.Time).Seconds())
	}
	r.Audit.Record(ControllerIdentity, "action.complete", "remediationaction/"+a.Name, "failed", map[string]any{"message": msg})
	r.Recorder.Event(a, corev1.EventTypeWarning, "Failed", msg)
	if !mayRevert || !a.Status.Applied || a.Status.Snapshot == nil || !a.Status.Snapshot.Reversible || a.Spec.ActionType == v1.ActionRevert {
		return ctrl.Result{}, nil
	}
	pol, _, err := r.Policy.Get(ctx)
	if err != nil || !pol.AutoRevertOnFailure {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, r.requestRevert(ctx, a, ControllerIdentity+" (auto-revert on failure)")
}

func (r *ActionReconciler) requestRevert(ctx context.Context, a *v1.RemediationAction, requester string) error {
	name := a.Name + "-revert"
	if len(name) > 253 {
		name = name[:253]
	}
	rv := &v1.RemediationAction{
		ObjectMeta: metav1.ObjectMeta{Namespace: a.Namespace, Name: name,
			Labels:      map[string]string{"aegisops.io/incident": a.Labels["aegisops.io/incident"], "aegisops.io/revert-of": a.Name},
			Annotations: map[string]string{AnnotationTraceParent: a.Annotations[AnnotationTraceParent]}},
		Spec: v1.RemediationActionSpec{IncidentID: a.Spec.IncidentID, ActionType: v1.ActionRevert, Target: a.Spec.Target,
			RevertOf: a.Name, RequestedBy: requester, Justification: "automatic revert: " + a.Status.Message},
	}
	err := r.Create(ctx, rv)
	if apierrors.IsAlreadyExists(err) {
		return nil
	}
	if err == nil {
		r.Audit.Record(requester, "action.revert.request", "remediationaction/"+a.Name, "requested", map[string]any{"revert": name})
	}
	return err
}

func (r *ActionReconciler) markRolledBack(ctx context.Context, orig *v1.RemediationAction, by string) error {
	orig.Status.Phase = v1.PhaseRolledBack
	orig.Status.RevertedBy = by
	orig.Status.Message = "reverted by " + by
	telemetry.ActionTransitions.WithLabelValues(string(orig.Spec.ActionType), string(v1.PhaseRolledBack)).Inc()
	if err := r.Status().Update(ctx, orig); err != nil {
		return err
	}
	r.Recorder.Event(orig, corev1.EventTypeNormal, "RolledBack", "pre-action state restored by "+by)
	return nil
}

func eventType(ok bool) string {
	if ok {
		return corev1.EventTypeNormal
	}
	return corev1.EventTypeWarning
}

// SetupWithManager registers the controller. Deployment changes wake up
// executing actions that target them (event-driven progress tracking).
func (r *ActionReconciler) SetupWithManager(mgr ctrl.Manager, systemNamespace string) error {
	mapDeployment := func(ctx context.Context, obj client.Object) []reconcile.Request {
		var list v1.RemediationActionList
		if err := r.List(ctx, &list, client.InNamespace(systemNamespace)); err != nil {
			return nil
		}
		var reqs []reconcile.Request
		for _, a := range list.Items {
			if a.Status.Phase == v1.PhaseExecuting && a.Spec.Target.Namespace == obj.GetNamespace() {
				reqs = append(reqs, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&a)})
			}
		}
		return reqs
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1.RemediationAction{}).
		Watches(&appsv1.Deployment{}, handler.EnqueueRequestsFromMapFunc(mapDeployment)).
		Named("remediationaction").
		Complete(r)
}
