// Package actions implements the executors for every allowlisted action. Each
// executor takes a snapshot before mutating (so the action can be reverted),
// applies a narrowly-scoped mutation through the Kubernetes API, and reports
// progress with rollout-status semantics.
package actions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

// FieldManager identifies AegisOps-originated writes in managedFields.
const FieldManager = "aegisops-controller"

// Snapshot kinds.
const (
	SnapDeployment   = "Deployment"
	SnapConfig       = "ConfigMap"
	SnapPod          = "Pod"
	SnapIsolation    = "PodIsolation"
	SnapNetPol       = "NetworkPolicy"
	SnapCanary       = "CanaryRelease"
	SnapIrreversible = "None"
)

// Progress reports execution state.
type Progress struct {
	Done    bool
	Failed  bool
	Message string
}

// Executor applies actions.
type Executor struct {
	Client   client.Client
	Resolver *state.Resolver
	History  *history.Store
	Now      func() time.Time
}

type deploymentSnapshot struct {
	Namespace string                 `json:"namespace"`
	Name      string                 `json:"name"`
	Replicas  int32                  `json:"replicas"`
	Paused    bool                   `json:"paused"`
	Template  corev1.PodTemplateSpec `json:"template"`
	// Ready replicas when the snapshot was taken: a revert that restores an already
	// unhealthy state is reported as restored-but-unhealthy, not as a failed revert.
	ReadyReplicas int32 `json:"readyReplicas"`
}

type configSnapshot struct {
	Deployment deploymentSnapshot `json:"deployment"`
	ConfigMap  string             `json:"configMap"`
	Data       map[string]string  `json:"data"`
	Revision   string             `json:"revision"`
	// RestoreRevision is the concrete revision update_config will write, resolved once when the
	// snapshot is taken. Resolving "previous" at apply time is not idempotent: the action's own
	// write appends to the history, so a re-apply would resolve "previous" to the faulty data.
	RestoreRevision string `json:"restoreRevision,omitempty"`
}

type podSnapshot struct {
	Namespace  string            `json:"namespace"`
	Name       string            `json:"name"`
	UID        types.UID         `json:"uid"`
	Deployment string            `json:"deployment"`
	Labels     map[string]string `json:"labels"`
}

type netpolSnapshot struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

type canarySnapshot struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Paused    bool   `json:"paused"`
}

func (x *Executor) now() time.Time {
	if x.Now != nil {
		return x.Now()
	}
	return time.Now()
}

func (x *Executor) deployment(ctx context.Context, ns, name string) (*appsv1.Deployment, error) {
	var d appsv1.Deployment
	if err := x.Client.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &d); err != nil {
		return nil, err
	}
	return &d, nil
}

func snapDeployment(d *appsv1.Deployment) deploymentSnapshot {
	s := deploymentSnapshot{Namespace: d.Namespace, Name: d.Name, Paused: d.Spec.Paused, Template: *d.Spec.Template.DeepCopy(),
		ReadyReplicas: d.Status.ReadyReplicas}
	if d.Spec.Replicas != nil {
		s.Replicas = *d.Spec.Replicas
	}
	return s
}

func encode(kind string, reversible bool, v any, now time.Time) (*v1.Snapshot, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return &v1.Snapshot{Kind: kind, Reversible: reversible, Data: string(b), TakenAt: metav1.NewTime(now)}, nil
}

// Snapshot captures the pre-action state. For revert_action the snapshot is
// irreversible (reverting a revert is not supported: escalate instead).
func (x *Executor) Snapshot(ctx context.Context, a *v1.RemediationAction) (*v1.Snapshot, error) {
	t := a.Spec.Target
	now := x.now()
	switch a.Spec.ActionType {
	case v1.ActionScaleDeployment, v1.ActionRollbackDeployment, v1.ActionPatchResources, v1.ActionRolloutRestart,
		v1.ActionApplyNetworkPolicy:
		if a.Spec.ActionType == v1.ActionApplyNetworkPolicy {
			return encode(SnapNetPol, true, netpolSnapshot{Namespace: t.Namespace, Name: netpolName(a)}, now)
		}
		d, err := x.deployment(ctx, t.Namespace, t.Name)
		if err != nil {
			return nil, err
		}
		return encode(SnapDeployment, a.Spec.ActionType != v1.ActionRolloutRestart, snapDeployment(d), now)
	case v1.ActionPauseRollout, v1.ActionResumeRollout:
		if t.Kind == "CanaryRelease" {
			var cr v1.CanaryRelease
			if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &cr); err != nil {
				return nil, err
			}
			return encode(SnapCanary, true, canarySnapshot{Namespace: t.Namespace, Name: t.Name, Paused: cr.Spec.Paused}, now)
		}
		d, err := x.deployment(ctx, t.Namespace, t.Name)
		if err != nil {
			return nil, err
		}
		return encode(SnapDeployment, true, snapDeployment(d), now)
	case v1.ActionUpdateConfig:
		d, err := x.deployment(ctx, t.Namespace, t.Name)
		if err != nil {
			return nil, err
		}
		var cm corev1.ConfigMap
		if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: a.Spec.Parameters.ConfigMap}, &cm); err != nil {
			return nil, err
		}
		target, err := x.History.Resolve(ctx, t.Namespace, a.Spec.Parameters.ConfigMap, a.Spec.Parameters.ConfigRevision)
		if err != nil {
			return nil, err
		}
		return encode(SnapConfig, true, configSnapshot{Deployment: snapDeployment(d), ConfigMap: cm.Name,
			Data: cm.Data, Revision: history.DataHash(cm.Data), RestoreRevision: target.Revision}, now)
	case v1.ActionRestartPod, v1.ActionIsolatePod:
		var pod corev1.Pod
		if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &pod); err != nil {
			return nil, err
		}
		kind, rev := SnapPod, false
		if a.Spec.ActionType == v1.ActionIsolatePod {
			kind, rev = SnapIsolation, true
		}
		ts, err := x.Resolver.Target(ctx, t, "", "")
		if err != nil {
			return nil, err
		}
		return encode(kind, rev, podSnapshot{Namespace: pod.Namespace, Name: pod.Name, UID: pod.UID, Deployment: ts.Deployment, Labels: pod.Labels}, now)
	case v1.ActionAbortCanary, v1.ActionRevert:
		return encode(SnapIrreversible, false, map[string]string{"target": t.Key()}, now)
	}
	return nil, fmt.Errorf("no snapshot strategy for %s", a.Spec.ActionType)
}

type expectationKey struct{}

// expectation carries an action's planned-against revision into updateDeployment,
// where it is checked atomically with the write (the Update is conditional on the
// resourceVersion that was read, so a concurrent rollout forces a re-check).
type expectation struct {
	action   string
	revision int64
}

func withExpectation(ctx context.Context, a *v1.RemediationAction) context.Context {
	if a.Spec.Preconditions == nil || a.Spec.Preconditions.ExpectedRevision == nil {
		return ctx
	}
	return context.WithValue(ctx, expectationKey{}, expectation{action: a.Name, revision: *a.Spec.Preconditions.ExpectedRevision})
}

// ErrStalePlan reports that the target no longer matches the state the action was planned against.
var ErrStalePlan = errors.New("stale plan")

func checkExpectation(ctx context.Context, d *appsv1.Deployment) error {
	exp, ok := ctx.Value(expectationKey{}).(expectation)
	if !ok || d.Annotations["aegisops.io/last-action"] == exp.action {
		return nil // no precondition, or this action already wrote the object (crash-safe re-apply)
	}
	if rev := state.Revision(d); rev != exp.revision {
		return fmt.Errorf("%w: %s/%s is at revision %d, action was planned against revision %d",
			ErrStalePlan, d.Namespace, d.Name, rev, exp.revision)
	}
	return nil
}

func (x *Executor) updateDeployment(ctx context.Context, ns, name string, mutate func(*appsv1.Deployment) error) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		d, err := x.deployment(ctx, ns, name)
		if err != nil {
			return err
		}
		if err := checkExpectation(ctx, d); err != nil {
			return err
		}
		if err := mutate(d); err != nil {
			return err
		}
		return x.Client.Update(ctx, d, client.FieldOwner(FieldManager))
	})
}

func (x *Executor) markTemplate(d *appsv1.Deployment, a *v1.RemediationAction) {
	if d.Annotations == nil {
		d.Annotations = map[string]string{}
	}
	d.Annotations["aegisops.io/last-action"] = a.Name
}

func (x *Executor) restartTemplate(d *appsv1.Deployment) {
	if d.Spec.Template.Annotations == nil {
		d.Spec.Template.Annotations = map[string]string{}
	}
	d.Spec.Template.Annotations[state.AnnotationRestartedAt] = x.now().UTC().Format(time.RFC3339)
}

// Apply performs the mutation. It is safe to call again after a crash
// (mutations are idempotent or converge to the same state).
func (x *Executor) Apply(ctx context.Context, a *v1.RemediationAction, original *v1.RemediationAction) (string, error) {
	t := a.Spec.Target
	p := a.Spec.Parameters
	ctx = withExpectation(ctx, a)
	if t.Kind == "Pod" {
		// Pod actions do not write the Deployment; check the owning rollout up front.
		if exp, ok := ctx.Value(expectationKey{}).(expectation); ok {
			ts, err := x.Resolver.Target(ctx, t, "", "")
			if err != nil {
				return "", err
			}
			if ts.CurrentRevision != exp.revision {
				return "", fmt.Errorf("%w: %s is at revision %d, action was planned against revision %d",
					ErrStalePlan, ts.Deployment, ts.CurrentRevision, exp.revision)
			}
		}
	}
	switch a.Spec.ActionType {
	case v1.ActionRestartPod:
		snap, err := decodePod(a.Status.Snapshot)
		if err != nil {
			return "", err
		}
		pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: t.Namespace, Name: t.Name}}
		err = x.Client.Delete(ctx, pod, client.Preconditions{UID: &snap.UID})
		if apierrors.IsNotFound(err) || apierrors.IsConflict(err) {
			return "pod already replaced", nil
		}
		return "deleted pod " + t.Name, err

	case v1.ActionRolloutRestart:
		return "rolling restart triggered", x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
			x.restartTemplate(d)
			x.markTemplate(d, a)
			return nil
		})

	case v1.ActionScaleDeployment:
		return fmt.Sprintf("scaled to %d replicas", *p.Replicas), x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
			d.Spec.Replicas = p.Replicas
			x.markTemplate(d, a)
			return nil
		})

	case v1.ActionRollbackDeployment:
		var msg string
		err := x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
			if d.Spec.Paused {
				return fmt.Errorf("deployment is paused; resume before rollback")
			}
			current := state.Revision(d)
			tmpl, rev, err := RollbackTemplate(ctx, x.Resolver, d, p.ToRevision)
			if err != nil {
				return err
			}
			d.Spec.Template = *tmpl
			x.markTemplate(d, a)
			d.Annotations["aegisops.io/rolled-back-to"] = fmt.Sprint(rev)
			msg = fmt.Sprintf("rolled back from revision %d to %d", current, rev)
			return nil
		})
		return msg, err

	case v1.ActionPatchResources:
		return "resources updated", x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
			if err := ApplyResources(&d.Spec.Template, p.Container, p.Resources); err != nil {
				return err
			}
			x.markTemplate(d, a)
			return nil
		})

	case v1.ActionPauseRollout, v1.ActionResumeRollout:
		paused := a.Spec.ActionType == v1.ActionPauseRollout
		if t.Kind == "CanaryRelease" {
			return fmt.Sprintf("canary paused=%v", paused), x.patchCanary(ctx, t, func(cr *v1.CanaryRelease) { cr.Spec.Paused = paused })
		}
		return fmt.Sprintf("rollout paused=%v", paused), x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
			d.Spec.Paused = paused
			x.markTemplate(d, a)
			return nil
		})

	case v1.ActionUpdateConfig:
		revision := p.ConfigRevision
		if a.Status.Snapshot != nil {
			var snap configSnapshot
			if err := json.Unmarshal([]byte(a.Status.Snapshot.Data), &snap); err == nil && snap.RestoreRevision != "" {
				revision = snap.RestoreRevision // pinned when the snapshot was taken; makes re-apply idempotent
			}
		}
		version, err := x.History.Resolve(ctx, t.Namespace, p.ConfigMap, revision)
		if err != nil {
			return "", err
		}
		if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
			var cm corev1.ConfigMap
			if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: p.ConfigMap}, &cm); err != nil {
				return err
			}
			cm.Data = version.Data
			return x.Client.Update(ctx, &cm, client.FieldOwner(FieldManager))
		}); err != nil {
			return "", err
		}
		return fmt.Sprintf("restored configmap %s to revision %s and restarted %s", p.ConfigMap, version.Revision, t.Name),
			x.updateDeployment(ctx, t.Namespace, t.Name, func(d *appsv1.Deployment) error {
				x.restartTemplate(d)
				x.markTemplate(d, a)
				return nil
			})

	case v1.ActionIsolatePod:
		return "pod isolated from service and replicaset", retry.RetryOnConflict(retry.DefaultRetry, func() error {
			var pod corev1.Pod
			if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &pod); err != nil {
				return err
			}
			if pod.Labels[state.LabelQuarantined] == "true" {
				return nil
			}
			ts, err := x.Resolver.Target(ctx, t, "", "")
			if err != nil {
				return err
			}
			d, err := x.deployment(ctx, t.Namespace, ts.Deployment)
			if err != nil {
				return err
			}
			for k := range d.Spec.Selector.MatchLabels {
				delete(pod.Labels, k)
			}
			delete(pod.Labels, "pod-template-hash")
			pod.Labels[state.LabelQuarantined] = "true"
			pod.Labels["aegisops.io/quarantined-from"] = d.Name
			return x.Client.Update(ctx, &pod, client.FieldOwner(FieldManager))
		})

	case v1.ActionApplyNetworkPolicy:
		d, err := x.deployment(ctx, t.Namespace, t.Name)
		if err != nil {
			return "", err
		}
		np := &networkingv1.NetworkPolicy{
			ObjectMeta: metav1.ObjectMeta{Namespace: t.Namespace, Name: netpolName(a),
				Labels: map[string]string{"app.kubernetes.io/managed-by": FieldManager, "aegisops.io/action": a.Name}},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: metav1.LabelSelector{MatchLabels: d.Spec.Selector.MatchLabels},
				PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			},
		}
		if p.NetworkPolicyTemplate == "allow-same-namespace-only" {
			np.Spec.Ingress = []networkingv1.NetworkPolicyIngressRule{{From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{}}}}}
		}
		err = x.Client.Create(ctx, np, client.FieldOwner(FieldManager))
		if apierrors.IsAlreadyExists(err) {
			err = nil
		}
		return "applied network policy " + np.Name, err

	case v1.ActionAbortCanary:
		return "canary abort requested", x.patchCanary(ctx, t, func(cr *v1.CanaryRelease) { cr.Spec.Abort = true })

	case v1.ActionRevert:
		if original == nil || original.Status.Snapshot == nil {
			return "", fmt.Errorf("original action or snapshot missing")
		}
		return x.revert(ctx, a, original)
	}
	return "", fmt.Errorf("no executor for %s", a.Spec.ActionType)
}

func (x *Executor) patchCanary(ctx context.Context, t v1.TargetRef, mutate func(*v1.CanaryRelease)) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var cr v1.CanaryRelease
		if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &cr); err != nil {
			return err
		}
		mutate(&cr)
		return x.Client.Update(ctx, &cr, client.FieldOwner(FieldManager))
	})
}

func (x *Executor) revert(ctx context.Context, a, original *v1.RemediationAction) (string, error) {
	snap := original.Status.Snapshot
	if !snap.Reversible {
		return "", fmt.Errorf("snapshot of %s is not reversible", original.Name)
	}
	switch snap.Kind {
	case SnapDeployment:
		var s deploymentSnapshot
		if err := json.Unmarshal([]byte(snap.Data), &s); err != nil {
			return "", err
		}
		return fmt.Sprintf("restored %s to pre-action state of %s", s.Name, original.Name),
			x.updateDeployment(ctx, s.Namespace, s.Name, func(d *appsv1.Deployment) error {
				r := s.Replicas
				d.Spec.Replicas = &r
				d.Spec.Paused = s.Paused
				d.Spec.Template = s.Template
				x.markTemplate(d, a)
				return nil
			})
	case SnapConfig:
		var s configSnapshot
		if err := json.Unmarshal([]byte(snap.Data), &s); err != nil {
			return "", err
		}
		if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
			var cm corev1.ConfigMap
			if err := x.Client.Get(ctx, client.ObjectKey{Namespace: s.Deployment.Namespace, Name: s.ConfigMap}, &cm); err != nil {
				return err
			}
			cm.Data = s.Data
			return x.Client.Update(ctx, &cm, client.FieldOwner(FieldManager))
		}); err != nil {
			return "", err
		}
		return fmt.Sprintf("restored configmap %s to revision %s", s.ConfigMap, s.Revision),
			x.updateDeployment(ctx, s.Deployment.Namespace, s.Deployment.Name, func(d *appsv1.Deployment) error {
				d.Spec.Template = s.Deployment.Template
				x.restartTemplate(d)
				x.markTemplate(d, a)
				return nil
			})
	case SnapIsolation:
		s, err := decodePod(snap)
		if err != nil {
			return "", err
		}
		pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: s.Namespace, Name: s.Name}}
		if err := x.Client.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
			return "", err
		}
		return "deleted quarantined pod " + s.Name, nil
	case SnapNetPol:
		var s netpolSnapshot
		if err := json.Unmarshal([]byte(snap.Data), &s); err != nil {
			return "", err
		}
		np := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Namespace: s.Namespace, Name: s.Name}}
		if err := x.Client.Delete(ctx, np); err != nil && !apierrors.IsNotFound(err) {
			return "", err
		}
		return "removed network policy " + s.Name, nil
	case SnapCanary:
		var s canarySnapshot
		if err := json.Unmarshal([]byte(snap.Data), &s); err != nil {
			return "", err
		}
		return "restored canary pause state", x.patchCanary(ctx, v1.TargetRef{Kind: "CanaryRelease", Namespace: s.Namespace, Name: s.Name},
			func(cr *v1.CanaryRelease) { cr.Spec.Paused = s.Paused })
	}
	return "", fmt.Errorf("unsupported snapshot kind %q", snap.Kind)
}

// Check reports whether the action's effect has converged.
func (x *Executor) Check(ctx context.Context, a *v1.RemediationAction, original *v1.RemediationAction) (Progress, error) {
	t := a.Spec.Target
	switch a.Spec.ActionType {
	case v1.ActionRestartPod:
		snap, err := decodePod(a.Status.Snapshot)
		if err != nil {
			return Progress{}, err
		}
		var pod corev1.Pod
		err = x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &pod)
		if err == nil && pod.UID == snap.UID {
			return Progress{Message: "waiting for pod termination"}, nil
		}
		if err != nil && !apierrors.IsNotFound(err) {
			return Progress{}, err
		}
		if snap.Deployment == "" {
			return Progress{Done: true, Message: "pod replaced"}, nil
		}
		return x.checkDeployment(ctx, t.Namespace, snap.Deployment, startedAt(a))
	case v1.ActionIsolatePod:
		var pod corev1.Pod
		if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &pod); err != nil {
			return Progress{}, err
		}
		if pod.Labels[state.LabelQuarantined] != "true" {
			return Progress{Message: "waiting for isolation labels"}, nil
		}
		return x.checkDeployment(ctx, t.Namespace, pod.Labels["aegisops.io/quarantined-from"], startedAt(a))
	case v1.ActionApplyNetworkPolicy:
		var np networkingv1.NetworkPolicy
		if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: netpolName(a)}, &np); err != nil {
			return Progress{Message: "waiting for network policy"}, client.IgnoreNotFound(err)
		}
		return Progress{Done: true, Message: "network policy active"}, nil
	case v1.ActionAbortCanary, v1.ActionPauseRollout, v1.ActionResumeRollout:
		if t.Kind == "CanaryRelease" {
			var cr v1.CanaryRelease
			if err := x.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &cr); err != nil {
				return Progress{}, err
			}
			want := map[v1.ActionType]v1.CanaryPhase{v1.ActionAbortCanary: v1.CanaryAborted, v1.ActionPauseRollout: v1.CanaryPaused,
				v1.ActionResumeRollout: v1.CanaryProgressing}[a.Spec.ActionType]
			if cr.Status.Phase == want || (a.Spec.ActionType == v1.ActionResumeRollout && cr.Status.Phase == v1.CanaryPromoted) {
				return Progress{Done: true, Message: "canary " + string(cr.Status.Phase)}, nil
			}
			return Progress{Message: "canary is " + string(cr.Status.Phase)}, nil
		}
		if a.Spec.ActionType == v1.ActionPauseRollout {
			return Progress{Done: true, Message: "rollout paused"}, nil
		}
		return x.checkDeployment(ctx, t.Namespace, t.Name, startedAt(a))
	case v1.ActionRevert:
		if original != nil && original.Status.Snapshot != nil {
			switch original.Status.Snapshot.Kind {
			case SnapNetPol, SnapCanary:
				return Progress{Done: true, Message: "reverted"}, nil
			case SnapIsolation:
				s, err := decodePod(original.Status.Snapshot)
				if err != nil {
					return Progress{}, err
				}
				var pod corev1.Pod
				err = x.Client.Get(ctx, client.ObjectKey{Namespace: s.Namespace, Name: s.Name}, &pod)
				if apierrors.IsNotFound(err) {
					return Progress{Done: true, Message: "quarantined pod removed"}, nil
				}
				return Progress{Message: "waiting for quarantined pod deletion"}, err
			}
		}
		prog, err := x.checkDeployment(ctx, t.Namespace, deploymentOf(t, original), startedAt(a))
		if err == nil && prog.Failed && original != nil && wasUnhealthy(original.Status.Snapshot) {
			// The revert put back exactly what was there before the reverted action, which was
			// already failing. The revert did its job; the incident is not fixed and escalates.
			return Progress{Done: true, Message: "restored the pre-action state, which was already unhealthy before the " +
				"reverted action (" + prog.Message + ")"}, nil
		}
		return prog, err
	default:
		return x.checkDeployment(ctx, t.Namespace, t.Name, startedAt(a))
	}
}

func (x *Executor) checkDeployment(ctx context.Context, ns, name string, since time.Time) (Progress, error) {
	d, err := x.deployment(ctx, ns, name)
	if err != nil {
		return Progress{}, err
	}
	done, failed, msg := state.RolloutStatus(d, since)
	if failed {
		return Progress{Failed: true, Message: msg}, nil
	}
	if !done {
		if p := x.crashLoopingNewPod(ctx, d, since); p != "" {
			return Progress{Failed: true, Message: p}, nil
		}
	}
	return Progress{Done: done, Message: msg}, nil
}

// crashLoopingNewPod reports a crash-looping pod that this rollout created, if any. Only the
// ReplicaSet of the Deployment's *current* revision counts, and only once the Deployment
// controller has observed the new spec: a fix that returns to an older template re-activates an
// older ReplicaSet, so "most recently created ReplicaSet" would point at the faulty one and its
// crashing pods. Pods created before the action started are pre-existing damage, not its effect.
func (x *Executor) crashLoopingNewPod(ctx context.Context, d *appsv1.Deployment, since time.Time) string {
	if d.Status.ObservedGeneration < d.Generation {
		return ""
	}
	rss, err := x.Resolver.OwnedReplicaSets(ctx, d)
	if err != nil {
		return ""
	}
	current := ""
	for i := range rss {
		if state.Revision(&rss[i]) == state.Revision(d) {
			current = rss[i].Labels["pod-template-hash"]
		}
	}
	if current == "" {
		return ""
	}
	pods, err := x.Resolver.PodsFor(ctx, d)
	if err != nil {
		return ""
	}
	for _, p := range pods {
		ps := state.PodSummary(p)
		if ps.PodTemplateHash == current && !p.CreationTimestamp.Time.Before(since) &&
			ps.WaitingReason == "CrashLoopBackOff" && ps.Restarts >= 3 {
			return fmt.Sprintf("new pod %s is crash-looping (%d restarts)", p.Name, ps.Restarts)
		}
	}
	return ""
}

// wasUnhealthy reports whether a Deployment snapshot was taken while replicas were not ready.
func wasUnhealthy(snap *v1.Snapshot) bool {
	if snap == nil {
		return false
	}
	// Pointers distinguish "0 ready" from snapshots taken before readiness was recorded.
	type health struct {
		Replicas      int32  `json:"replicas"`
		ReadyReplicas *int32 `json:"readyReplicas"`
	}
	var s struct {
		health
		Deployment *health `json:"deployment"`
	}
	if json.Unmarshal([]byte(snap.Data), &s) != nil {
		return false
	}
	h := s.health
	if s.Deployment != nil {
		h = *s.Deployment
	}
	return h.ReadyReplicas != nil && *h.ReadyReplicas < h.Replicas
}

// startedAt is when the action began executing (zero if unknown, which considers every condition).
func startedAt(a *v1.RemediationAction) time.Time {
	if a.Status.StartedAt == nil {
		return time.Time{}
	}
	return a.Status.StartedAt.Time
}

func setQty(rl corev1.ResourceList, name corev1.ResourceName, v string) {
	if v == "" {
		return
	}
	rl[name] = resource.MustParse(v)
}

func decodePod(s *v1.Snapshot) (*podSnapshot, error) {
	if s == nil {
		return nil, fmt.Errorf("missing snapshot")
	}
	var p podSnapshot
	if err := json.Unmarshal([]byte(s.Data), &p); err != nil {
		return nil, err
	}
	return &p, nil
}

func netpolName(a *v1.RemediationAction) string {
	n := "aegis-" + a.Spec.Parameters.NetworkPolicyTemplate + "-" + a.Spec.Target.Name
	if len(n) > 63 {
		n = n[:63]
	}
	return n
}

func deploymentOf(t v1.TargetRef, original *v1.RemediationAction) string {
	if original != nil && original.Status.Snapshot != nil {
		var s struct {
			Name       string             `json:"name"`
			Deployment deploymentSnapshot `json:"deployment"`
		}
		if json.Unmarshal([]byte(original.Status.Snapshot.Data), &s) == nil {
			if s.Deployment.Name != "" {
				return s.Deployment.Name
			}
			if original.Status.Snapshot.Kind == SnapDeployment && s.Name != "" {
				return s.Name
			}
		}
	}
	return t.Name
}
