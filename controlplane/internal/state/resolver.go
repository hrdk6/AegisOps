package state

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/labels"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
)

// ConfigHistory reports whether a recorded ConfigMap revision exists.
type ConfigHistory interface {
	HasRevision(ctx context.Context, namespace, name, revision string) bool
}

// Resolver reads cluster state from a (cached) client.
type Resolver struct {
	Client  client.Reader
	History ConfigHistory
}

var sensitiveKey = regexp.MustCompile(`(?i)(pass|secret|token|key|credential|auth)`)

// RedactEnvValue hides values of sensitive-looking variables.
func RedactEnvValue(name, value string) string {
	if sensitiveKey.MatchString(name) {
		return "[REDACTED]"
	}
	if len(value) > 256 {
		return value[:256] + "…"
	}
	return value
}

// Revision parses the deployment revision annotation.
func Revision(obj client.Object) int64 {
	v, _ := strconv.ParseInt(obj.GetAnnotations()[AnnotationRevision], 10, 64)
	return v
}

// SplitList parses a comma-separated annotation value.
func SplitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// TierOf returns the tier annotation (default standard).
func TierOf(d *appsv1.Deployment) string {
	if t := d.Annotations[AnnotationTier]; t != "" {
		return t
	}
	return policy.TierStandard
}

// OwnedReplicaSets returns ReplicaSets controlled by the deployment, newest revision first.
func (r *Resolver) OwnedReplicaSets(ctx context.Context, d *appsv1.Deployment) ([]appsv1.ReplicaSet, error) {
	var list appsv1.ReplicaSetList
	if err := r.Client.List(ctx, &list, client.InNamespace(d.Namespace)); err != nil {
		return nil, err
	}
	var out []appsv1.ReplicaSet
	for _, rs := range list.Items {
		for _, o := range rs.OwnerReferences {
			if o.Controller != nil && *o.Controller && o.UID == d.UID {
				out = append(out, rs)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return Revision(&out[i]) > Revision(&out[j]) })
	return out, nil
}

// PodsFor lists pods matching the deployment selector.
func (r *Resolver) PodsFor(ctx context.Context, d *appsv1.Deployment) ([]corev1.Pod, error) {
	sel := labels.SelectorFromSet(d.Spec.Selector.MatchLabels)
	var list corev1.PodList
	if err := r.Client.List(ctx, &list, client.InNamespace(d.Namespace), client.MatchingLabelsSelector{Selector: sel}); err != nil {
		return nil, err
	}
	return list.Items, nil
}

// ConfigMapsOf lists ConfigMaps referenced by a pod template.
func ConfigMapsOf(spec corev1.PodSpec) []string {
	seen := map[string]bool{}
	var out []string
	add := func(n string) {
		if n != "" && !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	for _, v := range spec.Volumes {
		if v.ConfigMap != nil {
			add(v.ConfigMap.Name)
		}
	}
	for _, c := range spec.Containers {
		for _, ef := range c.EnvFrom {
			if ef.ConfigMapRef != nil {
				add(ef.ConfigMapRef.Name)
			}
		}
		for _, e := range c.Env {
			if e.ValueFrom != nil && e.ValueFrom.ConfigMapKeyRef != nil {
				add(e.ValueFrom.ConfigMapKeyRef.Name)
			}
		}
	}
	return out
}

func dependentsOf(all []appsv1.Deployment, name string) []string {
	var out []string
	for _, d := range all {
		for _, dep := range SplitList(d.Annotations[AnnotationDependencies]) {
			if dep == name {
				out = append(out, d.Name)
			}
		}
	}
	sort.Strings(out)
	return out
}

// RolloutStatus mirrors `kubectl rollout status` semantics.
//
// A ProgressDeadlineExceeded condition last updated before `since` belongs to an earlier rollout
// and is ignored: when a rollback re-activates an existing ReplicaSet, the Deployment controller
// keeps the old condition until it observes progress, so treating it as current would fail every
// rollback of a release that had already timed out. Pass the zero time to consider all conditions.
func RolloutStatus(d *appsv1.Deployment, since time.Time) (done bool, failed bool, msg string) {
	if d.Generation > d.Status.ObservedGeneration {
		return false, false, "waiting for deployment spec update to be observed"
	}
	for _, c := range d.Status.Conditions {
		if c.Type == appsv1.DeploymentProgressing && c.Reason == "ProgressDeadlineExceeded" && !c.LastUpdateTime.Time.Before(since) {
			return false, true, fmt.Sprintf("deployment %q exceeded its progress deadline", d.Name)
		}
	}
	want := int32(1)
	if d.Spec.Replicas != nil {
		want = *d.Spec.Replicas
	}
	if d.Spec.Paused {
		return true, false, "deployment is paused"
	}
	if d.Status.UpdatedReplicas < want {
		return false, false, fmt.Sprintf("%d of %d updated replicas", d.Status.UpdatedReplicas, want)
	}
	if d.Status.Replicas > d.Status.UpdatedReplicas {
		return false, false, fmt.Sprintf("%d old replicas pending termination", d.Status.Replicas-d.Status.UpdatedReplicas)
	}
	if d.Status.AvailableReplicas < d.Status.UpdatedReplicas {
		return false, false, fmt.Sprintf("%d of %d updated replicas available", d.Status.AvailableReplicas, d.Status.UpdatedReplicas)
	}
	return true, false, "rollout complete"
}

// PodSummary converts a pod into its compact view.
func PodSummary(p corev1.Pod) Pod {
	out := Pod{
		Name: p.Name, Phase: string(p.Status.Phase), CreatedAt: p.CreationTimestamp.Time, Node: p.Spec.NodeName,
		PodTemplateHash: p.Labels["pod-template-hash"], Track: p.Labels[LabelTrack],
		Quarantined: p.Labels[LabelQuarantined] == "true",
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
			out.Ready = true
		}
	}
	for _, cs := range p.Status.ContainerStatuses {
		out.Restarts += cs.RestartCount
		if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" {
			out.WaitingReason = cs.State.Waiting.Reason
		}
		if t := cs.LastTerminationState.Terminated; t != nil {
			out.LastTerminationReason = t.Reason
			out.LastExitCode = t.ExitCode
			ft := t.FinishedAt.Time
			out.LastTerminationAt = &ft
		}
		if t := cs.State.Terminated; t != nil && out.LastTerminationReason == "" {
			out.LastTerminationReason = t.Reason
			out.LastExitCode = t.ExitCode
		}
	}
	return out
}

func quantities(rl corev1.ResourceList) map[string]string {
	out := map[string]string{}
	for k, v := range rl {
		out[string(k)] = v.String()
	}
	return out
}

// ContainerSummary produces a redacted container view.
func ContainerSummary(c corev1.Container) Container {
	env := map[string]string{}
	for _, e := range c.Env {
		switch {
		case e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil:
			env[e.Name] = "secretKeyRef:" + e.ValueFrom.SecretKeyRef.Name
		case e.ValueFrom != nil && e.ValueFrom.ConfigMapKeyRef != nil:
			env[e.Name] = "configMapKeyRef:" + e.ValueFrom.ConfigMapKeyRef.Name + "/" + e.ValueFrom.ConfigMapKeyRef.Key
		case e.ValueFrom != nil:
			env[e.Name] = "valueFrom"
		default:
			env[e.Name] = RedactEnvValue(e.Name, e.Value)
		}
	}
	var cms []string
	for _, ef := range c.EnvFrom {
		if ef.ConfigMapRef != nil {
			cms = append(cms, ef.ConfigMapRef.Name)
		}
	}
	return Container{Name: c.Name, Image: c.Image, Env: env, Requests: quantities(c.Resources.Requests),
		Limits: quantities(c.Resources.Limits), ConfigMap: cms}
}

// Workloads lists all Deployments in a namespace.
func (r *Resolver) Workloads(ctx context.Context, namespace string) ([]Workload, error) {
	var list appsv1.DeploymentList
	if err := r.Client.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		return nil, err
	}
	out := make([]Workload, 0, len(list.Items))
	for i := range list.Items {
		w, err := r.workload(ctx, &list.Items[i], list.Items)
		if err != nil {
			return nil, err
		}
		out = append(out, *w)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// Workload returns one Deployment's read model.
func (r *Resolver) Workload(ctx context.Context, namespace, name string) (*Workload, error) {
	var list appsv1.DeploymentList
	if err := r.Client.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		return nil, err
	}
	for i := range list.Items {
		if list.Items[i].Name == name {
			return r.workload(ctx, &list.Items[i], list.Items)
		}
	}
	return nil, apierrors.NewNotFound(appsv1.Resource("deployments"), name)
}

func (r *Resolver) workload(ctx context.Context, d *appsv1.Deployment, all []appsv1.Deployment) (*Workload, error) {
	done, _, msg := RolloutStatus(d, time.Time{})
	w := &Workload{
		Namespace: d.Namespace, Name: d.Name, Tier: TierOf(d), Owner: d.Annotations[AnnotationOwner],
		Dependencies: SplitList(d.Annotations[AnnotationDependencies]), Dependents: dependentsOf(all, d.Name),
		ReadyReplicas: d.Status.ReadyReplicas, AvailableReplicas: d.Status.AvailableReplicas,
		UpdatedReplicas: d.Status.UpdatedReplicas, Generation: d.Generation, ObservedGeneration: d.Status.ObservedGeneration,
		Paused: d.Spec.Paused, Revision: Revision(d), RolloutComplete: done, RolloutMessage: msg,
		Labels: d.Spec.Template.Labels, SimulationProfile: d.Annotations[AnnotationSimulationProfile],
		CreatedAt: d.CreationTimestamp.Time,
	}
	if d.Spec.Replicas != nil {
		w.Replicas = *d.Spec.Replicas
	}
	_, w.SimulationEnabled = d.Annotations[AnnotationSimulationProfile]
	if w.Dependencies == nil {
		w.Dependencies = []string{}
	}
	if w.Dependents == nil {
		w.Dependents = []string{}
	}
	for _, c := range d.Spec.Template.Spec.Containers {
		w.Containers = append(w.Containers, ContainerSummary(c))
	}
	pods, err := r.PodsFor(ctx, d)
	if err != nil {
		return nil, err
	}
	w.Pods = []Pod{}
	for _, p := range pods {
		w.Pods = append(w.Pods, PodSummary(p))
	}
	sort.Slice(w.Pods, func(i, j int) bool { return w.Pods[i].Name < w.Pods[j].Name })
	rss, err := r.OwnedReplicaSets(ctx, d)
	if err != nil {
		return nil, err
	}
	w.Revisions = []RolloutRevision{}
	for _, rs := range rss {
		rev := RolloutRevision{Revision: Revision(&rs), ReplicaSet: rs.Name, PodTemplateHash: rs.Labels["pod-template-hash"],
			CreatedAt: rs.CreationTimestamp.Time, Version: rs.Spec.Template.Labels["version"]}
		if rs.Spec.Replicas != nil {
			rev.Replicas = *rs.Spec.Replicas
		}
		for _, c := range rs.Spec.Template.Spec.Containers {
			rev.Images = append(rev.Images, c.Image)
		}
		w.Revisions = append(w.Revisions, rev)
	}
	return w, nil
}

// Topology returns the declared dependency graph of a namespace.
func (r *Resolver) Topology(ctx context.Context, namespace string) (*Topology, error) {
	var list appsv1.DeploymentList
	if err := r.Client.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		return nil, err
	}
	t := &Topology{Namespace: namespace, Tiers: map[string]string{}, Nodes: []TopologyNode{}, Edges: []Edge{}}
	for _, d := range list.Items {
		n := TopologyNode{Name: d.Name, Tier: TierOf(&d), Ready: d.Status.ReadyReplicas}
		if d.Spec.Replicas != nil {
			n.Replicas = *d.Spec.Replicas
		}
		t.Nodes = append(t.Nodes, n)
		t.Tiers[d.Name] = n.Tier
		for _, dep := range SplitList(d.Annotations[AnnotationDependencies]) {
			t.Edges = append(t.Edges, Edge{From: d.Name, To: dep})
		}
	}
	sort.Slice(t.Nodes, func(i, j int) bool { return t.Nodes[i].Name < t.Nodes[j].Name })
	return t, nil
}

// Target resolves the policy view of an action target.
func (r *Resolver) Target(ctx context.Context, t v1.TargetRef, configMap, configRevision string) (policy.TargetState, error) {
	ts := policy.TargetState{Kind: t.Kind, Namespace: t.Namespace, Name: t.Name}
	var depName string
	switch t.Kind {
	case "Deployment":
		depName = t.Name
	case "Pod":
		var pod corev1.Pod
		if err := r.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &pod); err != nil {
			if apierrors.IsNotFound(err) {
				return ts, nil
			}
			return ts, err
		}
		dep, err := r.ownerDeployment(ctx, &pod)
		if err != nil {
			return ts, err
		}
		if dep == "" {
			ts.Exists = true // exists but unowned: prerequisites check denies
			return ts, nil
		}
		depName = dep
	case "CanaryRelease":
		var cr v1.CanaryRelease
		if err := r.Client.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name}, &cr); err != nil {
			if apierrors.IsNotFound(err) {
				return ts, nil
			}
			return ts, err
		}
		ts.CanaryPhase = cr.Status.Phase
		if ts.CanaryPhase == "" {
			ts.CanaryPhase = v1.CanaryPending
		}
		depName = cr.Spec.TargetRef
	default:
		return ts, fmt.Errorf("unsupported target kind %q", t.Kind)
	}

	var list appsv1.DeploymentList
	if err := r.Client.List(ctx, &list, client.InNamespace(t.Namespace)); err != nil {
		return ts, err
	}
	var d *appsv1.Deployment
	for i := range list.Items {
		if list.Items[i].Name == depName {
			d = &list.Items[i]
		}
	}
	if d == nil {
		return ts, nil
	}
	ts.Exists = true
	ts.Deployment = d.Name
	if d.Spec.Replicas != nil {
		ts.Replicas = *d.Spec.Replicas
	}
	ts.Ready = d.Status.ReadyReplicas
	ts.Tier = TierOf(d)
	ts.Dependents = dependentsOf(list.Items, d.Name)
	ts.CurrentRevision = Revision(d)
	ts.Paused = d.Spec.Paused
	done, _, _ := RolloutStatus(d, time.Time{})
	ts.RolloutInProgress = !done
	ts.ConfigMaps = ConfigMapsOf(d.Spec.Template.Spec)
	for _, c := range d.Spec.Template.Spec.Containers {
		ts.Containers = append(ts.Containers, c.Name)
	}
	rss, err := r.OwnedReplicaSets(ctx, d)
	if err != nil {
		return ts, err
	}
	for _, rs := range rss {
		ts.Revisions = append(ts.Revisions, Revision(&rs))
	}
	if configMap != "" && r.History != nil {
		rev := configRevision
		if rev == "" {
			rev = "previous"
		}
		ts.ConfigRevisionAvailable = r.History.HasRevision(ctx, t.Namespace, configMap, rev)
	}
	return ts, nil
}

func (r *Resolver) ownerDeployment(ctx context.Context, pod *corev1.Pod) (string, error) {
	for _, o := range pod.OwnerReferences {
		if o.Kind != "ReplicaSet" || o.Controller == nil || !*o.Controller {
			continue
		}
		var rs appsv1.ReplicaSet
		if err := r.Client.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: o.Name}, &rs); err != nil {
			if apierrors.IsNotFound(err) {
				return "", nil
			}
			return "", err
		}
		for _, ro := range rs.OwnerReferences {
			if ro.Kind == "Deployment" && ro.Controller != nil && *ro.Controller {
				return ro.Name, nil
			}
		}
	}
	return "", nil
}
