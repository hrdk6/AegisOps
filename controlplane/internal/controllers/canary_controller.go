package controllers

import (
	"context"
	"fmt"
	"math"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/actions"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/promclient"
	"github.com/aegisops/aegisops/controlplane/internal/state"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

// CanaryQueries names the metrics used for canary analysis.
type CanaryQueries struct {
	RequestsMetric string // counter with labels namespace, service, track, code
	DurationMetric string // histogram base name (without _bucket)
}

// DefaultCanaryQueries match the demo application's instrumentation.
var DefaultCanaryQueries = CanaryQueries{
	RequestsMetric: "shop_http_requests_total",
	DurationMetric: "shop_http_request_duration_seconds",
}

const (
	maxInconclusive    = 3
	canaryReadyTimeout = 120 * time.Second
)

// CanaryReconciler implements replica-ratio progressive delivery with
// deterministic SLO gates (the Argo Rollouts "basic canary" model).
type CanaryReconciler struct {
	client.Client
	Recorder record.EventRecorder
	Prom     *promclient.Client
	Queries  CanaryQueries
	Audit    *audit.Log
	Now      func() time.Time
}

func (r *CanaryReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// WeightReplicas converts a traffic weight to a canary replica count.
func WeightReplicas(total, weight int32) int32 {
	c := int32(math.Round(float64(total) * float64(weight) / 100))
	if c < 1 {
		c = 1
	}
	if c > total {
		c = total
	}
	return c
}

func canaryName(cr *v1.CanaryRelease) string { return cr.Spec.TargetRef + "-canary" }

// Reconcile implements reconcile.Reconciler.
func (r *CanaryReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var cr v1.CanaryRelease
	if err := r.Get(ctx, req.NamespacedName, &cr); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	var (
		res ctrl.Result
		err error
	)
	switch cr.Status.Phase {
	case "", v1.CanaryPending:
		res, err = r.initialize(ctx, &cr)
	case v1.CanaryProgressing:
		res, err = r.progress(ctx, &cr)
	case v1.CanaryPaused:
		switch {
		case cr.Spec.Abort:
			res, err = r.abort(ctx, &cr, "abort requested")
		case !cr.Spec.Paused:
			cr.Status.Phase = v1.CanaryProgressing
			cr.Status.StepStartedAt = nil
			cr.Status.Message = "resumed"
			err = r.Status().Update(ctx, &cr)
		}
	case v1.CanaryPromoting:
		res, err = r.promote(ctx, &cr)
	}
	if apierrors.IsConflict(err) {
		return ctrl.Result{Requeue: true}, nil
	}
	return res, err
}

func (r *CanaryReconciler) setFailed(ctx context.Context, cr *v1.CanaryRelease, msg string) (ctrl.Result, error) {
	cr.Status.Phase = v1.CanaryFailed
	cr.Status.Message = msg
	now := metav1.NewTime(r.now())
	cr.Status.CompletedAt = &now
	r.Recorder.Event(cr, corev1.EventTypeWarning, "Failed", msg)
	return ctrl.Result{}, r.Status().Update(ctx, cr)
}

func (r *CanaryReconciler) initialize(ctx context.Context, cr *v1.CanaryRelease) (ctrl.Result, error) {
	var stable appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: cr.Spec.TargetRef}, &stable); err != nil {
		if apierrors.IsNotFound(err) {
			return r.setFailed(ctx, cr, "target deployment not found")
		}
		return ctrl.Result{}, err
	}
	if _, ok := stable.Spec.Selector.MatchLabels[state.LabelTrack]; !ok {
		return r.setFailed(ctx, cr, "stable selector must include the 'track' label so canary pods are isolated")
	}
	var others v1.CanaryReleaseList
	if err := r.List(ctx, &others, client.InNamespace(cr.Namespace)); err != nil {
		return ctrl.Result{}, err
	}
	for _, o := range others.Items {
		if o.Name != cr.Name && o.Spec.TargetRef == cr.Spec.TargetRef &&
			(o.Status.Phase == v1.CanaryProgressing || o.Status.Phase == v1.CanaryPaused || o.Status.Phase == v1.CanaryPromoting) {
			return r.setFailed(ctx, cr, "another canary is active for "+cr.Spec.TargetRef)
		}
	}
	orig := int32(1)
	if stable.Spec.Replicas != nil {
		orig = *stable.Spec.Replicas
	}
	canary := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: cr.Namespace, Name: canaryName(cr),
		Labels: map[string]string{"app.kubernetes.io/managed-by": "aegisops-controller", "aegisops.io/canary-of": stable.Name}}}
	sel := map[string]string{}
	for k, v := range stable.Spec.Selector.MatchLabels {
		sel[k] = v
	}
	sel[state.LabelTrack] = "canary"
	tmpl := stable.Spec.Template.DeepCopy()
	for k, v := range sel {
		tmpl.Labels[k] = v
	}
	applyRelease(tmpl, cr.Spec.Release)
	zero := int32(0)
	canary.Spec = appsv1.DeploymentSpec{Replicas: &zero, Selector: &metav1.LabelSelector{MatchLabels: sel}, Template: *tmpl}
	canary.Annotations = map[string]string{state.AnnotationTier: state.TierOf(&stable)}
	if err := controllerutil.SetControllerReference(cr, canary, r.Scheme()); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.Create(ctx, canary, client.FieldOwner(actions.FieldManager)); err != nil && !apierrors.IsAlreadyExists(err) {
		return r.setFailed(ctx, cr, "cannot create canary deployment: "+err.Error())
	}
	cr.Status.Phase = v1.CanaryProgressing
	cr.Status.OriginalReplicas = orig
	cr.Status.CurrentStep = 0
	cr.Status.Message = fmt.Sprintf("canary %s created for release %s", canary.Name, cr.Spec.Release.Version)
	r.Audit.Record(ControllerIdentity, "canary.start", "canaryrelease/"+cr.Namespace+"/"+cr.Name, "progressing",
		map[string]any{"target": cr.Spec.TargetRef, "version": cr.Spec.Release.Version})
	r.Recorder.Event(cr, corev1.EventTypeNormal, "Started", cr.Status.Message)
	return ctrl.Result{RequeueAfter: time.Second}, r.Status().Update(ctx, cr)
}

func applyRelease(tmpl *corev1.PodTemplateSpec, rel v1.CanaryTemplate) {
	if tmpl.Labels == nil {
		tmpl.Labels = map[string]string{}
	}
	if rel.Version != "" {
		tmpl.Labels["version"] = rel.Version
	}
	if len(tmpl.Spec.Containers) == 0 {
		return
	}
	c := &tmpl.Spec.Containers[0]
	if rel.Image != "" {
		c.Image = rel.Image
	}
	for _, e := range rel.Env {
		found := false
		for i := range c.Env {
			if c.Env[i].Name == e.Name {
				c.Env[i] = corev1.EnvVar{Name: e.Name, Value: e.Value}
				found = true
			}
		}
		if !found {
			c.Env = append(c.Env, corev1.EnvVar{Name: e.Name, Value: e.Value})
		}
	}
}

func (r *CanaryReconciler) scale(ctx context.Context, ns, name string, replicas int32) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var d appsv1.Deployment
		if err := r.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &d); err != nil {
			return err
		}
		if d.Spec.Replicas != nil && *d.Spec.Replicas == replicas {
			return nil
		}
		d.Spec.Replicas = &replicas
		return r.Update(ctx, &d, client.FieldOwner(actions.FieldManager))
	})
}

func (r *CanaryReconciler) total(cr *v1.CanaryRelease) int32 {
	if cr.Spec.TotalReplicas > 0 {
		return cr.Spec.TotalReplicas
	}
	if cr.Status.OriginalReplicas < 2 {
		return 2
	}
	return cr.Status.OriginalReplicas
}

func (r *CanaryReconciler) progress(ctx context.Context, cr *v1.CanaryRelease) (ctrl.Result, error) {
	if cr.Spec.Abort {
		return r.abort(ctx, cr, "abort requested")
	}
	if cr.Spec.Paused {
		cr.Status.Phase = v1.CanaryPaused
		cr.Status.Message = "paused"
		return ctrl.Result{}, r.Status().Update(ctx, cr)
	}
	if int(cr.Status.CurrentStep) >= len(cr.Spec.Steps) || cr.Spec.Steps[cr.Status.CurrentStep].Weight >= 100 {
		cr.Status.Phase = v1.CanaryPromoting
		cr.Status.Message = "all analysis steps passed; promoting"
		return ctrl.Result{RequeueAfter: time.Second}, r.Status().Update(ctx, cr)
	}
	step := cr.Spec.Steps[cr.Status.CurrentStep]
	total := r.total(cr)
	canaryReplicas := WeightReplicas(total, step.Weight)
	stableReplicas := total - canaryReplicas
	if stableReplicas < 1 {
		stableReplicas = 1
	}
	if err := r.scale(ctx, cr.Namespace, canaryName(cr), canaryReplicas); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.scale(ctx, cr.Namespace, cr.Spec.TargetRef, stableReplicas); err != nil {
		return ctrl.Result{}, err
	}
	var canary appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: canaryName(cr)}, &canary); err != nil {
		return ctrl.Result{}, err
	}
	cr.Status.CanaryReplicas = canaryReplicas
	cr.Status.StableReplicas = stableReplicas
	cr.Status.EffectiveWeight = int32(math.Round(100 * float64(canaryReplicas) / float64(canaryReplicas+stableReplicas)))
	if cr.Status.StepStartedAt == nil {
		if canary.Status.AvailableReplicas < canaryReplicas {
			waitingSince := cr.CreationTimestamp.Time
			if n := len(cr.Status.Analyses); n > 0 {
				waitingSince = cr.Status.Analyses[n-1].Time.Time
			}
			if r.now().Sub(waitingSince) > canaryReadyTimeout {
				return r.abort(ctx, cr, "canary pods did not become available")
			}
			cr.Status.Message = fmt.Sprintf("step %d: waiting for %d canary pods", cr.Status.CurrentStep, canaryReplicas)
			return ctrl.Result{RequeueAfter: 3 * time.Second}, r.Status().Update(ctx, cr)
		}
		now := metav1.NewTime(r.now())
		cr.Status.StepStartedAt = &now
		cr.Status.Message = fmt.Sprintf("step %d: holding at %d%% for %ds", cr.Status.CurrentStep, cr.Status.EffectiveWeight, step.PauseSeconds)
		return ctrl.Result{RequeueAfter: time.Duration(step.PauseSeconds) * time.Second}, r.Status().Update(ctx, cr)
	}
	hold := time.Duration(step.PauseSeconds) * time.Second
	if elapsed := r.now().Sub(cr.Status.StepStartedAt.Time); elapsed < hold {
		return ctrl.Result{RequeueAfter: hold - elapsed}, nil
	}

	result := r.analyze(ctx, cr, step)
	cr.Status.Analyses = append(cr.Status.Analyses, result)
	telemetry.CanaryAnalyses.WithLabelValues(result.Verdict).Inc()
	switch result.Verdict {
	case "Fail":
		return r.abort(ctx, cr, "analysis failed: "+result.Reason)
	case "Inconclusive":
		if consecutiveInconclusive(cr.Status.Analyses) >= maxInconclusive {
			return r.abort(ctx, cr, "analysis inconclusive too many times (fail closed): "+result.Reason)
		}
		cr.Status.Message = "analysis inconclusive: " + result.Reason
		return ctrl.Result{RequeueAfter: 10 * time.Second}, r.Status().Update(ctx, cr)
	}
	cr.Status.CurrentStep++
	cr.Status.StepStartedAt = nil
	cr.Status.Message = fmt.Sprintf("step passed at %d%%: %s", cr.Status.EffectiveWeight, result.Reason)
	r.Recorder.Event(cr, corev1.EventTypeNormal, "StepPassed", cr.Status.Message)
	return ctrl.Result{RequeueAfter: time.Second}, r.Status().Update(ctx, cr)
}

func consecutiveInconclusive(a []v1.CanaryAnalysisResult) int {
	n := 0
	for i := len(a) - 1; i >= 0 && a[i].Verdict == "Inconclusive"; i-- {
		n++
	}
	return n
}

func (r *CanaryReconciler) query(ctx context.Context, q string) (float64, bool) {
	v, ok, err := r.Prom.Scalar(ctx, q)
	if err != nil {
		return 0, false
	}
	return v, ok
}

func (r *CanaryReconciler) analyze(ctx context.Context, cr *v1.CanaryRelease, step v1.CanaryStep) v1.CanaryAnalysisResult {
	window := step.PauseSeconds
	if window > 60 {
		window = 60
	}
	svc := cr.Spec.TargetRef
	sel := func(track string) string {
		t := `track="canary"`
		if track != "canary" {
			t = `track!="canary"`
		}
		return fmt.Sprintf(`namespace=%q,service=%q,%s`, cr.Namespace, svc, t)
	}
	errQ := func(track string) string {
		return fmt.Sprintf(`sum(rate(%s{%s,code=~"5.."}[%ds])) / sum(rate(%s{%s}[%ds]))`,
			r.Queries.RequestsMetric, sel(track), window, r.Queries.RequestsMetric, sel(track), window)
	}
	p95Q := func(track string) string {
		return fmt.Sprintf(`histogram_quantile(0.95, sum by (le) (rate(%s_bucket{%s}[%ds]))) * 1000`, r.Queries.DurationMetric, sel(track), window)
	}
	res := v1.CanaryAnalysisResult{Step: cr.Status.CurrentStep, Weight: cr.Status.EffectiveWeight, Time: metav1.NewTime(r.now())}
	reqs, ok := r.query(ctx, fmt.Sprintf(`sum(increase(%s{%s}[%ds]))`, r.Queries.RequestsMetric, sel("canary"), window))
	if !ok {
		res.Verdict, res.Reason = "Inconclusive", "no canary request metrics"
		return res
	}
	res.CanaryRequests = reqs
	res.CanaryErrorRate, _ = r.query(ctx, errQ("canary"))
	res.StableErrorRate, _ = r.query(ctx, errQ("stable"))
	res.CanaryP95Ms, _ = r.query(ctx, p95Q("canary"))
	res.StableP95Ms, _ = r.query(ctx, p95Q("stable"))
	a := cr.Spec.Analysis
	switch {
	case reqs < float64(a.MinRequests):
		res.Verdict, res.Reason = "Inconclusive", fmt.Sprintf("only %.0f canary requests (< %d)", reqs, a.MinRequests)
	case res.CanaryErrorRate > a.MaxErrorRate:
		res.Verdict, res.Reason = "Fail", fmt.Sprintf("canary error rate %.2f%% > %.2f%% (stable %.2f%%)",
			res.CanaryErrorRate*100, a.MaxErrorRate*100, res.StableErrorRate*100)
	case a.MaxP95Ms > 0 && res.CanaryP95Ms > a.MaxP95Ms:
		res.Verdict, res.Reason = "Fail", fmt.Sprintf("canary p95 %.0fms > %.0fms (stable %.0fms)", res.CanaryP95Ms, a.MaxP95Ms, res.StableP95Ms)
	default:
		res.Verdict, res.Reason = "Pass", fmt.Sprintf("error %.2f%%, p95 %.0fms over %.0f requests", res.CanaryErrorRate*100, res.CanaryP95Ms, reqs)
	}
	return res
}

func (r *CanaryReconciler) abort(ctx context.Context, cr *v1.CanaryRelease, reason string) (ctrl.Result, error) {
	if err := r.scale(ctx, cr.Namespace, cr.Spec.TargetRef, cr.Status.OriginalReplicas); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	canary := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: cr.Namespace, Name: canaryName(cr)}}
	if err := r.Delete(ctx, canary); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	cr.Status.Phase = v1.CanaryAborted
	cr.Status.Message = reason
	cr.Status.CanaryReplicas = 0
	cr.Status.EffectiveWeight = 0
	now := metav1.NewTime(r.now())
	cr.Status.CompletedAt = &now
	r.Audit.Record(ControllerIdentity, "canary.abort", "canaryrelease/"+cr.Namespace+"/"+cr.Name, "aborted", map[string]any{"reason": reason})
	r.Recorder.Event(cr, corev1.EventTypeWarning, "Aborted", reason)
	return ctrl.Result{}, r.Status().Update(ctx, cr)
}

func (r *CanaryReconciler) promote(ctx context.Context, cr *v1.CanaryRelease) (ctrl.Result, error) {
	var stable appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: cr.Spec.TargetRef}, &stable); err != nil {
		return ctrl.Result{}, err
	}
	if stable.Spec.Template.Labels["version"] != cr.Spec.Release.Version || (cr.Spec.Release.Version == "" && stable.Annotations["aegisops.io/promoted"] != cr.Name) {
		err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
			if err := r.Get(ctx, client.ObjectKeyFromObject(&stable), &stable); err != nil {
				return err
			}
			applyRelease(&stable.Spec.Template, cr.Spec.Release)
			rep := cr.Status.OriginalReplicas
			stable.Spec.Replicas = &rep
			if stable.Annotations == nil {
				stable.Annotations = map[string]string{}
			}
			stable.Annotations["aegisops.io/promoted"] = cr.Name
			return r.Update(ctx, &stable, client.FieldOwner(actions.FieldManager))
		})
		return ctrl.Result{RequeueAfter: 3 * time.Second}, err
	}
	if done, failed, msg := state.RolloutStatus(&stable, time.Time{}); failed {
		return r.abort(ctx, cr, "promotion rollout failed: "+msg)
	} else if !done {
		cr.Status.Message = "promoting: " + msg
		return ctrl.Result{RequeueAfter: 3 * time.Second}, r.Status().Update(ctx, cr)
	}
	canary := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: cr.Namespace, Name: canaryName(cr)}}
	if err := r.Delete(ctx, canary); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	cr.Status.Phase = v1.CanaryPromoted
	cr.Status.EffectiveWeight = 100
	cr.Status.Message = "release " + cr.Spec.Release.Version + " promoted to stable"
	now := metav1.NewTime(r.now())
	cr.Status.CompletedAt = &now
	r.Audit.Record(ControllerIdentity, "canary.promote", "canaryrelease/"+cr.Namespace+"/"+cr.Name, "promoted", nil)
	r.Recorder.Event(cr, corev1.EventTypeNormal, "Promoted", cr.Status.Message)
	return ctrl.Result{}, r.Status().Update(ctx, cr)
}

// SetupWithManager registers the controller.
func (r *CanaryReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).For(&v1.CanaryRelease{}).Owns(&appsv1.Deployment{}).Named("canaryrelease").Complete(r)
}
