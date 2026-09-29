package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/actions"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/hashing"
	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/registry"
	"github.com/aegisops/aegisops/controlplane/internal/sandbox"
	"github.com/aegisops/aegisops/controlplane/internal/state"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

const sandboxFinalizer = "aegisops.io/sandbox-cleanup"

// Simulation timing bounds.
const (
	readyTimeout = 75 * time.Second
	runTimeout   = 150 * time.Second
)

// SimulationReconciler runs RemediationSimulations in the sandbox namespace.
type SimulationReconciler struct {
	client.Client
	Recorder record.EventRecorder
	Policy   *PolicyProvider
	Resolver *state.Resolver
	History  *history.Store
	Sandbox  sandbox.Config
	Audit    *audit.Log
	Now      func() time.Time
}

func (r *SimulationReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// Reconcile implements reconcile.Reconciler.
func (r *SimulationReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var sim v1.RemediationSimulation
	if err := r.Get(ctx, req.NamespacedName, &sim); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !sim.DeletionTimestamp.IsZero() {
		if err := r.cleanup(ctx, sim.Name); err != nil {
			return ctrl.Result{}, err
		}
		controllerutil.RemoveFinalizer(&sim, sandboxFinalizer)
		return ctrl.Result{}, r.Update(ctx, &sim)
	}
	var (
		res ctrl.Result
		err error
	)
	switch sim.Status.Phase {
	case "", v1.SimPending:
		res, err = r.provision(ctx, &sim)
	case v1.SimProvisioning:
		res, err = r.waitReady(ctx, &sim)
	case v1.SimRunning:
		res, err = r.collect(ctx, &sim)
	}
	if apierrors.IsConflict(err) {
		return ctrl.Result{Requeue: true}, nil
	}
	return res, err
}

func (r *SimulationReconciler) finish(ctx context.Context, sim *v1.RemediationSimulation, phase v1.SimulationPhase, verdict, reason string) (ctrl.Result, error) {
	sim.Status.Phase = phase
	sim.Status.Verdict = verdict
	sim.Status.Reason = reason
	now := metav1.NewTime(r.now())
	sim.Status.CompletedAt = &now
	sim.Status.ObservedGeneration = sim.Generation
	if err := r.Status().Update(ctx, sim); err != nil {
		return ctrl.Result{}, err
	}
	telemetry.Simulations.WithLabelValues(string(phase), verdict).Inc()
	r.Audit.Record(ControllerIdentity, "simulation.complete", "remediationsimulation/"+sim.Name, string(phase),
		map[string]any{"verdict": verdict, "reason": reason})
	r.Recorder.Eventf(sim, corev1.EventTypeNormal, string(phase), "%s %s", verdict, reason)
	if err := r.cleanup(ctx, sim.Name); err != nil {
		log.FromContext(ctx).Error(err, "sandbox cleanup failed", "simulation", sim.Name)
	}
	return ctrl.Result{}, nil
}

// candidate computes the proposed template and ConfigMap data.
func (r *SimulationReconciler) variants(ctx context.Context, sim *v1.RemediationSimulation, d *appsv1.Deployment) ([]sandbox.Variant, error) {
	cmData := map[string]map[string]string{}
	for _, name := range state.ConfigMapsOf(d.Spec.Template.Spec) {
		var cm corev1.ConfigMap
		if err := r.Get(ctx, client.ObjectKey{Namespace: d.Namespace, Name: name}, &cm); err != nil {
			return nil, fmt.Errorf("read configmap %s: %w", name, err)
		}
		cmData[name] = cm.Data
	}
	baseline := sandbox.Variant{Name: sandbox.Baseline, Template: *d.Spec.Template.DeepCopy(), ConfigData: cmData}
	cand := sandbox.Variant{Name: sandbox.Candidate, Template: *d.Spec.Template.DeepCopy(), ConfigData: map[string]map[string]string{}}
	for k, v := range cmData {
		cand.ConfigData[k] = v
	}
	p := sim.Spec.Proposal.Parameters
	switch sim.Spec.Proposal.ActionType {
	case v1.ActionRollbackDeployment:
		tmpl, _, err := actions.RollbackTemplate(ctx, r.Resolver, d, p.ToRevision)
		if err != nil {
			return nil, err
		}
		cand.Template = *tmpl
		// A rolled-back template may reference different ConfigMaps.
		for _, name := range state.ConfigMapsOf(tmpl.Spec) {
			if _, ok := cand.ConfigData[name]; !ok {
				var cm corev1.ConfigMap
				if err := r.Get(ctx, client.ObjectKey{Namespace: d.Namespace, Name: name}, &cm); err != nil {
					return nil, err
				}
				cand.ConfigData[name] = cm.Data
			}
		}
	case v1.ActionPatchResources:
		if err := actions.ApplyResources(&cand.Template, p.Container, p.Resources); err != nil {
			return nil, err
		}
	case v1.ActionUpdateConfig:
		if _, ok := cmData[p.ConfigMap]; !ok {
			return nil, fmt.Errorf("configmap %q not consumed by %s", p.ConfigMap, d.Name)
		}
		v, err := r.History.Resolve(ctx, d.Namespace, p.ConfigMap, p.ConfigRevision)
		if err != nil {
			return nil, err
		}
		cand.ConfigData[p.ConfigMap] = v.Data
	default:
		return nil, fmt.Errorf("action %s is not simulatable", sim.Spec.Proposal.ActionType)
	}
	return []sandbox.Variant{baseline, cand}, nil
}

func (r *SimulationReconciler) provision(ctx context.Context, sim *v1.RemediationSimulation) (ctrl.Result, error) {
	spec := sim.Spec
	sim.Status.ProposalHash = hashing.ProposalHash(spec.Target, spec.Proposal.ActionType, spec.Proposal.Parameters)
	unavailable := func(reason string) (ctrl.Result, error) {
		return r.finish(ctx, sim, v1.SimUnavailable, "", reason)
	}
	reg, ok := registry.Lookup(spec.Proposal.ActionType)
	if !ok || !reg.Simulatable {
		return unavailable(fmt.Sprintf("action %s cannot be meaningfully simulated; use policy-only evaluation", spec.Proposal.ActionType))
	}
	if err := registry.ValidateStructure(spec.Proposal.ActionType, spec.Target, spec.Proposal.Parameters, ""); err != nil {
		return unavailable("invalid proposal: " + err.Error())
	}
	pol, _, err := r.Policy.Get(ctx)
	if err != nil {
		return ctrl.Result{}, err
	}
	allowed := false
	for _, ns := range pol.AllowedNamespaces {
		allowed = allowed || ns == spec.Target.Namespace
	}
	if !allowed {
		return unavailable("target namespace outside automation scope")
	}
	var d appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: spec.Target.Namespace, Name: spec.Target.Name}, &d); err != nil {
		if apierrors.IsNotFound(err) {
			return unavailable("target deployment not found")
		}
		return ctrl.Result{}, err
	}
	profileRaw, opted := d.Annotations[state.AnnotationSimulationProfile]
	if !opted {
		return unavailable("workload has not opted into sandbox simulation (aegisops.io/simulation-profile)")
	}
	probe, err := sandbox.ParseProbe(d.Annotations[state.AnnotationSimulationProbe])
	if err != nil {
		return unavailable(err.Error())
	}
	_ = probe

	// One simulation at a time keeps the sandbox inside its resource quota.
	var list v1.RemediationSimulationList
	if err := r.List(ctx, &list, client.InNamespace(sim.Namespace)); err != nil {
		return ctrl.Result{}, err
	}
	for _, other := range list.Items {
		if other.Name != sim.Name && (other.Status.Phase == v1.SimProvisioning || other.Status.Phase == v1.SimRunning) {
			if sim.Status.Phase != v1.SimPending {
				sim.Status.Phase = v1.SimPending
				sim.Status.Reason = "queued behind " + other.Name
				if err := r.Status().Update(ctx, sim); err != nil {
					return ctrl.Result{}, err
				}
			}
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
	}

	variants, err := r.variants(ctx, sim, &d)
	if err != nil {
		return unavailable("cannot build candidate: " + err.Error())
	}
	if !controllerutil.ContainsFinalizer(sim, sandboxFinalizer) {
		controllerutil.AddFinalizer(sim, sandboxFinalizer)
		if err := r.Update(ctx, sim); err != nil {
			return ctrl.Result{}, err
		}
	}
	for _, obj := range sandbox.Objects(sim.Name, variants, state.SplitList(profileRaw), r.Sandbox) {
		if err := r.Create(ctx, obj); err != nil && !apierrors.IsAlreadyExists(err) {
			_ = r.cleanup(ctx, sim.Name)
			return r.finish(ctx, sim, v1.SimFailed, "", "sandbox provisioning failed: "+err.Error())
		}
	}
	start := metav1.NewTime(r.now())
	// Set after the finalizer Update above: Update refreshes the object from the API server and
	// would discard status fields that were not yet persisted.
	sim.Status.ProposalHash = hashing.ProposalHash(spec.Target, spec.Proposal.ActionType, spec.Proposal.Parameters)
	sim.Status.StartedAt = &start
	sim.Status.Phase = v1.SimProvisioning
	sim.Status.SandboxNamespace = r.Sandbox.Namespace
	sim.Status.Reason = "provisioning baseline and candidate clones"
	if err := r.Status().Update(ctx, sim); err != nil {
		return ctrl.Result{}, err
	}
	r.Audit.Record(sim.Spec.RequestedBy, "simulation.start", "remediationsimulation/"+sim.Name, "provisioning",
		map[string]any{"target": spec.Target.Key(), "proposal": spec.Proposal.ActionType})
	return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
}

func (r *SimulationReconciler) ready(ctx context.Context, sim, variant string) bool {
	var d appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: r.Sandbox.Namespace, Name: sandbox.ObjectName(sim, variant)}, &d); err != nil {
		return false
	}
	return d.Status.AvailableReplicas >= 1
}

func (r *SimulationReconciler) waitReady(ctx context.Context, sim *v1.RemediationSimulation) (ctrl.Result, error) {
	base, cand := r.ready(ctx, sim.Name, sandbox.Baseline), r.ready(ctx, sim.Name, sandbox.Candidate)
	elapsed := r.now().Sub(sim.Status.StartedAt.Time)
	if !(base && cand) && elapsed < readyTimeout {
		return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
	}
	if !base && !cand {
		return r.finish(ctx, sim, v1.SimCompleted, v1.VerdictInconclusive, "neither baseline nor candidate became ready in the sandbox")
	}
	var d appsv1.Deployment
	if err := r.Get(ctx, client.ObjectKey{Namespace: sim.Spec.Target.Namespace, Name: sim.Spec.Target.Name}, &d); err != nil {
		return r.finish(ctx, sim, v1.SimFailed, "", "target disappeared: "+err.Error())
	}
	probe, err := sandbox.ParseProbe(d.Annotations[state.AnnotationSimulationProbe])
	if err != nil {
		return r.finish(ctx, sim, v1.SimUnavailable, "", err.Error())
	}
	if sim.Spec.Load.Path != "" {
		probe.Path, probe.Method, probe.Body = sim.Spec.Load.Path, sim.Spec.Load.Method, sim.Spec.Load.Body
	}
	job := sandbox.ProbeJob(sim.Name, sandbox.ContainerPort(d.Spec.Template), probe, sim.Spec.Load, r.Sandbox)
	if err := r.Create(ctx, job); err != nil && !apierrors.IsAlreadyExists(err) {
		return r.finish(ctx, sim, v1.SimFailed, "", "probe job creation failed: "+err.Error())
	}
	sim.Status.Phase = v1.SimRunning
	sim.Status.Reason = fmt.Sprintf("load probe running (baselineReady=%v candidateReady=%v)", base, cand)
	if err := r.Status().Update(ctx, sim); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
}

func (r *SimulationReconciler) collect(ctx context.Context, sim *v1.RemediationSimulation) (ctrl.Result, error) {
	var job batchv1.Job
	err := r.Get(ctx, client.ObjectKey{Namespace: r.Sandbox.Namespace, Name: sandbox.ObjectName(sim.Name, "probe")}, &job)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return r.finish(ctx, sim, v1.SimFailed, "", "probe job disappeared")
		}
		return ctrl.Result{}, err
	}
	if r.now().Sub(sim.Status.StartedAt.Time) > readyTimeout+runTimeout {
		return r.finish(ctx, sim, v1.SimFailed, "", "simulation timed out")
	}
	if job.Status.Succeeded == 0 && job.Status.Failed == 0 {
		return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
	}
	var pods corev1.PodList
	if err := r.List(ctx, &pods, client.InNamespace(r.Sandbox.Namespace), client.MatchingLabels{"job-name": job.Name}); err != nil {
		return ctrl.Result{}, err
	}
	var msg string
	for _, p := range pods.Items {
		for _, cs := range p.Status.ContainerStatuses {
			if cs.State.Terminated != nil && cs.State.Terminated.Message != "" {
				msg = cs.State.Terminated.Message
			}
		}
	}
	if msg == "" {
		if job.Status.Failed > 0 {
			return r.finish(ctx, sim, v1.SimFailed, "", "load probe failed without results")
		}
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	var res sandbox.ProbeResult
	if err := json.Unmarshal([]byte(msg), &res); err != nil {
		return r.finish(ctx, sim, v1.SimFailed, "", "unparseable probe result: "+err.Error())
	}
	verdict, summary := sandbox.Verdict(res.Baseline, res.Candidate)
	sim.Status.Baseline = &res.Baseline
	sim.Status.Candidate = &res.Candidate
	sim.Status.Summary = summary
	return r.finish(ctx, sim, v1.SimCompleted, verdict, summary)
}

// cleanup deletes every sandbox object of a simulation.
func (r *SimulationReconciler) cleanup(ctx context.Context, sim string) error {
	sel := client.MatchingLabels{sandbox.LabelSimulation: sim}
	ns := client.InNamespace(r.Sandbox.Namespace)
	bg := client.PropagationPolicy(metav1.DeletePropagationBackground)
	for _, list := range []client.ObjectList{&batchv1.JobList{}, &appsv1.DeploymentList{}, &corev1.ServiceList{}, &corev1.ConfigMapList{}} {
		if err := r.List(ctx, list, ns, sel); err != nil {
			return err
		}
		items, err := apimeta.ExtractList(list)
		if err != nil {
			return err
		}
		for _, item := range items {
			obj, ok := item.(client.Object)
			if !ok {
				continue
			}
			if err := r.Delete(ctx, obj, bg); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		}
	}
	return nil
}

// SetupWithManager registers the controller.
func (r *SimulationReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).For(&v1.RemediationSimulation{}).Named("remediationsimulation").Complete(r)
}
