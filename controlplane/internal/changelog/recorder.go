// Package changelog records what changed in the watched namespaces and when:
// Deployment spec changes (image, env, resources, replicas, restarts),
// ConfigMap data changes, container restarts/terminations and Kubernetes
// Events. This is the evidence source for "what deployment or configuration
// change happened immediately before the degradation?".
package changelog

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	toolscache "k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/cache"

	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

// FieldChange is one changed field.
type FieldChange struct {
	Path string `json:"path"`
	Old  string `json:"old"`
	New  string `json:"new"`
}

// Change is a recorded spec/config change.
type Change struct {
	Seq        int64         `json:"seq"`
	ID         string        `json:"id"`
	Time       time.Time     `json:"time"`
	Namespace  string        `json:"namespace"`
	Kind       string        `json:"kind"`
	Name       string        `json:"name"`
	Type       string        `json:"type"`
	Summary    string        `json:"summary"`
	Fields     []FieldChange `json:"fields"`
	Generation int64         `json:"generation,omitempty"`
	Actor      string        `json:"actor,omitempty"`
}

// Signal is a Kubernetes Event or a synthesized pod-status transition.
type Signal struct {
	Seq       int64     `json:"seq"`
	Time      time.Time `json:"time"`
	Namespace string    `json:"namespace"`
	Kind      string    `json:"kind"`
	Name      string    `json:"name"`
	Type      string    `json:"type"`
	Reason    string    `json:"reason"`
	Message   string    `json:"message"`
	Count     int32     `json:"count"`
	Source    string    `json:"source"`
}

// Recorder keeps bounded rings of changes and signals.
type Recorder struct {
	mu         sync.RWMutex
	namespaces map[string]bool
	changes    []Change
	signals    []Signal
	max        int
	seq        int64
	history    *history.Store
	histQueue  chan *corev1.ConfigMap
	log        logr.Logger
}

// NewRecorder creates a recorder for the given namespaces.
func NewRecorder(namespaces []string, h *history.Store, max int, log logr.Logger) *Recorder {
	ns := map[string]bool{}
	for _, n := range namespaces {
		ns[n] = true
	}
	return &Recorder{namespaces: ns, max: max, history: h, histQueue: make(chan *corev1.ConfigMap, 256), log: log.WithName("changelog")}
}

// Register wires informer event handlers. Call once, before or after cache start.
func (r *Recorder) Register(ctx context.Context, c cache.Cache) error {
	depInf, err := c.GetInformer(ctx, &appsv1.Deployment{})
	if err != nil {
		return err
	}
	if _, err := depInf.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
		AddFunc: func(obj any) {
			if d, ok := obj.(*appsv1.Deployment); ok && r.watched(d.Namespace) && time.Since(d.CreationTimestamp.Time) < time.Minute {
				r.addChange(Change{Namespace: d.Namespace, Kind: "Deployment", Name: d.Name, Type: "created",
					Summary: fmt.Sprintf("deployment %s created", d.Name), Generation: d.Generation, Actor: actor(d.ManagedFields)})
			}
		},
		UpdateFunc: func(oldObj, newObj any) {
			o, ok1 := oldObj.(*appsv1.Deployment)
			n, ok2 := newObj.(*appsv1.Deployment)
			if ok1 && ok2 && r.watched(n.Namespace) && o.Generation != n.Generation {
				r.onDeployment(o, n)
			}
		},
	}); err != nil {
		return err
	}

	cmInf, err := c.GetInformer(ctx, &corev1.ConfigMap{})
	if err != nil {
		return err
	}
	if _, err := cmInf.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
		AddFunc: func(obj any) {
			if cm, ok := obj.(*corev1.ConfigMap); ok && r.watched(cm.Namespace) {
				r.enqueueHistory(cm)
			}
		},
		UpdateFunc: func(oldObj, newObj any) {
			o, ok1 := oldObj.(*corev1.ConfigMap)
			n, ok2 := newObj.(*corev1.ConfigMap)
			if ok1 && ok2 && r.watched(n.Namespace) && history.DataHash(o.Data) != history.DataHash(n.Data) {
				r.onConfigMap(o, n)
				r.enqueueHistory(n)
			}
		},
	}); err != nil {
		return err
	}

	podInf, err := c.GetInformer(ctx, &corev1.Pod{})
	if err != nil {
		return err
	}
	if _, err := podInf.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
		UpdateFunc: func(oldObj, newObj any) {
			o, ok1 := oldObj.(*corev1.Pod)
			n, ok2 := newObj.(*corev1.Pod)
			if ok1 && ok2 && r.watched(n.Namespace) {
				r.onPod(o, n)
			}
		},
	}); err != nil {
		return err
	}

	evInf, err := c.GetInformer(ctx, &corev1.Event{})
	if err != nil {
		return err
	}
	handleEvent := func(obj any) {
		ev, ok := obj.(*corev1.Event)
		if !ok || !r.watched(ev.Namespace) {
			return
		}
		ts := ev.LastTimestamp.Time
		if ts.IsZero() {
			ts = ev.EventTime.Time
		}
		if ts.IsZero() {
			ts = ev.CreationTimestamp.Time
		}
		if time.Since(ts) > 30*time.Minute {
			return
		}
		r.addSignal(Signal{Time: ts, Namespace: ev.Namespace, Kind: ev.InvolvedObject.Kind, Name: ev.InvolvedObject.Name,
			Type: ev.Type, Reason: ev.Reason, Message: truncate(ev.Message, 400), Count: ev.Count, Source: "event"})
	}
	if _, err := evInf.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
		AddFunc:    handleEvent,
		UpdateFunc: func(_, n any) { handleEvent(n) },
	}); err != nil {
		return err
	}

	go r.drainHistory(ctx)
	return nil
}

func (r *Recorder) watched(ns string) bool { return r.namespaces[ns] }

func (r *Recorder) enqueueHistory(cm *corev1.ConfigMap) {
	if r.history == nil {
		return
	}
	select {
	case r.histQueue <- cm.DeepCopy():
	default:
		r.log.Info("history queue full; dropping config revision", "configmap", cm.Namespace+"/"+cm.Name)
	}
}

func (r *Recorder) drainHistory(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case cm := <-r.histQueue:
			if err := r.history.Record(ctx, cm); err != nil {
				r.log.Error(err, "record config history", "configmap", cm.Namespace+"/"+cm.Name)
			}
		}
	}
}

func (r *Recorder) addChange(c Change) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	c.Seq = r.seq
	c.ID = fmt.Sprintf("chg-%d-%d", time.Now().Unix(), r.seq)
	if c.Time.IsZero() {
		c.Time = time.Now().UTC()
	}
	if c.Fields == nil {
		c.Fields = []FieldChange{}
	}
	r.changes = append(r.changes, c)
	if len(r.changes) > r.max {
		r.changes = r.changes[len(r.changes)-r.max:]
	}
	r.log.Info("change recorded", "kind", c.Kind, "name", c.Namespace+"/"+c.Name, "type", c.Type, "summary", c.Summary, "actor", c.Actor)
}

func (r *Recorder) addSignal(s Signal) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	s.Seq = r.seq
	r.signals = append(r.signals, s)
	if len(r.signals) > r.max {
		r.signals = r.signals[len(r.signals)-r.max:]
	}
}

// Changes returns changes at or after since, optionally filtered.
func (r *Recorder) Changes(since time.Time, namespace, name string) []Change {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := []Change{}
	for _, c := range r.changes {
		if c.Time.Before(since) || (namespace != "" && c.Namespace != namespace) || (name != "" && c.Name != name) {
			continue
		}
		out = append(out, c)
	}
	return out
}

// Signals returns signals at or after since, optionally filtered.
func (r *Recorder) Signals(since time.Time, namespace, name string) []Signal {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := []Signal{}
	for _, s := range r.signals {
		if s.Time.Before(since) || (namespace != "" && s.Namespace != namespace) || (name != "" && !strings.HasPrefix(s.Name, name)) {
			continue
		}
		out = append(out, s)
	}
	return out
}

func (r *Recorder) onDeployment(o, n *appsv1.Deployment) {
	fields := DiffDeployment(o, n)
	if len(fields) == 0 {
		return
	}
	typ := classify(fields)
	parts := make([]string, 0, len(fields))
	for _, f := range fields {
		parts = append(parts, fmt.Sprintf("%s: %s -> %s", f.Path, orNone(f.Old), orNone(f.New)))
	}
	r.addChange(Change{Namespace: n.Namespace, Kind: "Deployment", Name: n.Name, Type: typ,
		Summary: truncate(n.Name+" "+typ+" change: "+strings.Join(parts, "; "), 600), Fields: fields,
		Generation: n.Generation, Actor: actor(n.ManagedFields)})
}

func (r *Recorder) onConfigMap(o, n *corev1.ConfigMap) {
	var fields []FieldChange
	keys := map[string]bool{}
	for k := range o.Data {
		keys[k] = true
	}
	for k := range n.Data {
		keys[k] = true
	}
	sorted := make([]string, 0, len(keys))
	for k := range keys {
		sorted = append(sorted, k)
	}
	sort.Strings(sorted)
	for _, k := range sorted {
		if o.Data[k] != n.Data[k] {
			fields = append(fields, FieldChange{Path: "data." + k,
				Old: truncate(state.RedactEnvValue(k, o.Data[k]), 300), New: truncate(state.RedactEnvValue(k, n.Data[k]), 300)})
		}
	}
	r.addChange(Change{Namespace: n.Namespace, Kind: "ConfigMap", Name: n.Name, Type: "config",
		Summary: fmt.Sprintf("configmap %s changed (%d keys; revision %s -> %s)", n.Name, len(fields), history.DataHash(o.Data), history.DataHash(n.Data)),
		Fields:  fields, Actor: actor(n.ManagedFields)})
}

func (r *Recorder) onPod(o, n *corev1.Pod) {
	prev := map[string]corev1.ContainerStatus{}
	for _, cs := range o.Status.ContainerStatuses {
		prev[cs.Name] = cs
	}
	for _, cs := range n.Status.ContainerStatuses {
		p := prev[cs.Name]
		if cs.RestartCount > p.RestartCount {
			reason, msg := "ContainerRestarted", fmt.Sprintf("container %s restarted (count %d)", cs.Name, cs.RestartCount)
			if t := cs.LastTerminationState.Terminated; t != nil {
				reason = t.Reason
				msg = fmt.Sprintf("container %s terminated: reason=%s exitCode=%d (restart %d)", cs.Name, t.Reason, t.ExitCode, cs.RestartCount)
			}
			r.addSignal(Signal{Time: time.Now().UTC(), Namespace: n.Namespace, Kind: "Pod", Name: n.Name, Type: "Warning",
				Reason: reason, Message: msg, Count: cs.RestartCount, Source: "pod-status"})
		}
		wasCrash := p.State.Waiting != nil && p.State.Waiting.Reason == "CrashLoopBackOff"
		if cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff" && !wasCrash {
			r.addSignal(Signal{Time: time.Now().UTC(), Namespace: n.Namespace, Kind: "Pod", Name: n.Name, Type: "Warning",
				Reason: "CrashLoopBackOff", Message: truncate(cs.State.Waiting.Message, 300), Count: cs.RestartCount, Source: "pod-status"})
		}
	}
}

// DiffDeployment returns the operationally relevant spec differences.
func DiffDeployment(o, n *appsv1.Deployment) []FieldChange {
	var out []FieldChange
	if rep(o) != rep(n) {
		out = append(out, FieldChange{Path: "spec.replicas", Old: fmt.Sprint(rep(o)), New: fmt.Sprint(rep(n))})
	}
	if o.Spec.Paused != n.Spec.Paused {
		out = append(out, FieldChange{Path: "spec.paused", Old: fmt.Sprint(o.Spec.Paused), New: fmt.Sprint(n.Spec.Paused)})
	}
	for _, key := range []string{state.AnnotationRestartedAt, "kubectl.kubernetes.io/restartedAt"} {
		if o.Spec.Template.Annotations[key] != n.Spec.Template.Annotations[key] {
			out = append(out, FieldChange{Path: "template.annotations." + key, Old: o.Spec.Template.Annotations[key], New: n.Spec.Template.Annotations[key]})
		}
	}
	if o.Spec.Template.Labels["version"] != n.Spec.Template.Labels["version"] {
		out = append(out, FieldChange{Path: "template.labels.version", Old: o.Spec.Template.Labels["version"], New: n.Spec.Template.Labels["version"]})
	}
	oc := map[string]corev1.Container{}
	for _, c := range o.Spec.Template.Spec.Containers {
		oc[c.Name] = c
	}
	for _, c := range n.Spec.Template.Spec.Containers {
		prev, ok := oc[c.Name]
		if !ok {
			out = append(out, FieldChange{Path: "containers." + c.Name, Old: "", New: "added"})
			continue
		}
		if prev.Image != c.Image {
			out = append(out, FieldChange{Path: "containers." + c.Name + ".image", Old: prev.Image, New: c.Image})
		}
		pe, ne := envMap(prev), envMap(c)
		for _, k := range unionKeys(pe, ne) {
			if pe[k] != ne[k] {
				out = append(out, FieldChange{Path: "containers." + c.Name + ".env." + k,
					Old: state.RedactEnvValue(k, pe[k]), New: state.RedactEnvValue(k, ne[k])})
			}
		}
		for _, r := range []struct {
			path string
			o, n corev1.ResourceList
		}{{"limits", prev.Resources.Limits, c.Resources.Limits}, {"requests", prev.Resources.Requests, c.Resources.Requests}} {
			for _, res := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
				ov, nv := r.o[res], r.n[res]
				if ov.Cmp(nv) != 0 {
					out = append(out, FieldChange{Path: fmt.Sprintf("containers.%s.resources.%s.%s", c.Name, r.path, res),
						Old: qty(r.o, res), New: qty(r.n, res)})
				}
			}
		}
	}
	return out
}

func classify(fields []FieldChange) string {
	has := func(sub string) bool {
		for _, f := range fields {
			if strings.Contains(f.Path, sub) {
				return true
			}
		}
		return false
	}
	switch {
	case has(".image") || has("labels.version"):
		return "release"
	case has(".env."):
		return "env"
	case has(".resources."):
		return "resources"
	case has("restartedAt"):
		return "restart"
	case has("spec.replicas"):
		return "scale"
	case has("spec.paused"):
		return "paused"
	}
	return "template"
}

func rep(d *appsv1.Deployment) int32 {
	if d.Spec.Replicas == nil {
		return 1
	}
	return *d.Spec.Replicas
}

func envMap(c corev1.Container) map[string]string {
	m := map[string]string{}
	for _, e := range c.Env {
		if e.ValueFrom != nil {
			m[e.Name] = "<ref>"
		} else {
			m[e.Name] = e.Value
		}
	}
	return m
}

func unionKeys(a, b map[string]string) []string {
	seen := map[string]bool{}
	for k := range a {
		seen[k] = true
	}
	for k := range b {
		seen[k] = true
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func qty(rl corev1.ResourceList, name corev1.ResourceName) string {
	if v, ok := rl[name]; ok {
		return v.String()
	}
	return ""
}

func actor(mf []metav1.ManagedFieldsEntry) string {
	var best string
	var bestTime time.Time
	for _, e := range mf {
		if e.Manager == "kube-controller-manager" || e.Subresource == "status" {
			continue
		}
		if e.Time != nil && e.Time.Time.After(bestTime) {
			bestTime = e.Time.Time
			best = e.Manager
		}
	}
	return best
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "â€¦"
}

func orNone(s string) string {
	if s == "" {
		return "<none>"
	}
	return s
}
