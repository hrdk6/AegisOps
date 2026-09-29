// Package sandbox builds the isolated "digital twin" used to A/B test a
// proposed remediation before it touches the real workload.
//
// For a simulation the controller creates, in the dedicated sandbox namespace
// (default-deny network policy, resource quota, no-permission service account):
//   - a baseline clone of the workload's *current* pod template,
//   - a candidate clone with the proposed change applied,
//   - per-pod sidecar dependencies (e.g. postgres, redis) declared by the
//     workload's aegisops.io/simulation-profile annotation, and
//   - a load-probe Job that replays identical synthetic traffic against both.
//
// Clones are sanitized: Secret references and persistent volumes are removed,
// the service account is replaced, and AEGIS_SANDBOX=1 tells the application
// to use its local sidecars and stub downstream HTTP calls.
package sandbox

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

// Labels used on every sandbox object.
const (
	LabelSimulation = "aegisops.io/simulation"
	LabelVariant    = "aegisops.io/variant"
	Baseline        = "baseline"
	Candidate       = "candidate"
)

// Config holds sandbox images and identity.
type Config struct {
	Namespace      string
	PostgresImage  string
	RedisImage     string
	ProbeImage     string
	ProbeCommand   []string
	ServiceAccount string
}

// Variant is one side of the A/B test.
type Variant struct {
	Name       string
	Template   corev1.PodTemplateSpec
	ConfigData map[string]map[string]string // source ConfigMap name -> data
}

// ProbeSpec is the (annotation-declared) request used for load testing.
type ProbeSpec struct {
	Path   string `json:"path"`
	Method string `json:"method"`
	Body   string `json:"body,omitempty"`
}

// ParseProbe reads the aegisops.io/simulation-probe annotation.
func ParseProbe(raw string) (ProbeSpec, error) {
	var p ProbeSpec
	if raw == "" {
		return p, fmt.Errorf("workload has no simulation probe annotation")
	}
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		return p, fmt.Errorf("invalid simulation probe annotation: %w", err)
	}
	if !strings.HasPrefix(p.Path, "/") || (p.Method != "GET" && p.Method != "POST") {
		return p, fmt.Errorf("invalid simulation probe: path must start with / and method be GET or POST")
	}
	return p, nil
}

// ObjectName builds a DNS-1123 compliant, length-bounded name.
func ObjectName(parts ...string) string {
	n := strings.ToLower(strings.Join(parts, "-"))
	if len(n) <= 63 {
		return n
	}
	sum := sha256.Sum256([]byte(n))
	return n[:50] + "-" + hex.EncodeToString(sum[:])[:12]
}

func labels(sim, variant string) map[string]string {
	l := map[string]string{LabelSimulation: sim, "app.kubernetes.io/managed-by": "aegisops-controller"}
	if variant != "" {
		l[LabelVariant] = variant
	}
	return l
}

// ContainerPort returns the first declared container port (default 8080).
func ContainerPort(t corev1.PodTemplateSpec) int32 {
	for _, c := range t.Spec.Containers {
		for _, p := range c.Ports {
			return p.ContainerPort
		}
	}
	return 8080
}

// Sanitize converts a production pod template into a sandbox-safe clone.
func Sanitize(t corev1.PodTemplateSpec, sim, variant string, cmRename map[string]string, profile []string, cfg Config) corev1.PodTemplateSpec {
	out := *t.DeepCopy()
	out.Labels = labels(sim, variant)
	out.Annotations = map[string]string{"aegisops.io/sandbox": "true"}
	s := &out.Spec
	s.ServiceAccountName = cfg.ServiceAccount
	f := false
	s.AutomountServiceAccountToken = &f
	s.Affinity = nil
	s.NodeSelector = nil
	s.TopologySpreadConstraints = nil
	s.ImagePullSecrets = nil

	// Drop secret and persistent volumes; rename ConfigMap volumes.
	dropped := map[string]bool{}
	var vols []corev1.Volume
	for _, v := range s.Volumes {
		switch {
		case v.Secret != nil, v.Projected != nil:
			dropped[v.Name] = true
		case v.PersistentVolumeClaim != nil:
			vols = append(vols, corev1.Volume{Name: v.Name, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		case v.ConfigMap != nil:
			v.ConfigMap.Name = cmRename[v.ConfigMap.Name]
			vols = append(vols, v)
		default:
			vols = append(vols, v)
		}
	}
	s.Volumes = vols

	for i := range s.Containers {
		c := &s.Containers[i]
		var env []corev1.EnvVar
		for _, e := range c.Env {
			if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
				continue
			}
			if e.ValueFrom != nil && e.ValueFrom.ConfigMapKeyRef != nil {
				e.ValueFrom.ConfigMapKeyRef.Name = cmRename[e.ValueFrom.ConfigMapKeyRef.Name]
			}
			if e.Name == "AEGIS_SANDBOX" {
				continue
			}
			env = append(env, e)
		}
		env = append(env, corev1.EnvVar{Name: "AEGIS_SANDBOX", Value: "1"}, corev1.EnvVar{Name: "OTEL_SDK_DISABLED", Value: "true"})
		c.Env = env
		var envFrom []corev1.EnvFromSource
		for _, ef := range c.EnvFrom {
			if ef.SecretRef != nil {
				continue
			}
			if ef.ConfigMapRef != nil {
				ef.ConfigMapRef.Name = cmRename[ef.ConfigMapRef.Name]
			}
			envFrom = append(envFrom, ef)
		}
		c.EnvFrom = envFrom
		var mounts []corev1.VolumeMount
		for _, m := range c.VolumeMounts {
			if !dropped[m.Name] {
				mounts = append(mounts, m)
			}
		}
		c.VolumeMounts = mounts
	}
	s.InitContainers = nil
	for _, dep := range profile {
		switch dep {
		case "postgres":
			s.Containers = append(s.Containers, corev1.Container{
				Name: "sandbox-postgres", Image: cfg.PostgresImage,
				Env: []corev1.EnvVar{{Name: "POSTGRES_USER", Value: "shop"}, {Name: "POSTGRES_PASSWORD", Value: "sandbox"},
					{Name: "POSTGRES_DB", Value: "shop"}, {Name: "PGDATA", Value: "/tmp/pgdata"}},
				Resources: resources("150m", "192Mi"),
				// The clone inherits a non-root pod identity; run as the image's postgres user so initdb
				// owns its data directory.
				SecurityContext: &corev1.SecurityContext{RunAsUser: ptr(int64(70)), RunAsGroup: ptr(int64(70)),
					RunAsNonRoot: ptr(true), AllowPrivilegeEscalation: ptr(false)},
			})
		case "redis":
			s.Containers = append(s.Containers, corev1.Container{
				Name: "sandbox-redis", Image: cfg.RedisImage, Args: []string{"--save", "", "--appendonly", "no"},
				Resources: resources("50m", "64Mi"),
			})
		}
	}
	return out
}

func resources(cpu, mem string) corev1.ResourceRequirements {
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("20m"), corev1.ResourceMemory: resource.MustParse("32Mi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(mem)},
	}
}

// Objects returns all sandbox objects for both variants.
func Objects(sim string, variants []Variant, profile []string, cfg Config) []client.Object {
	var objs []client.Object
	for _, v := range variants {
		rename := map[string]string{}
		for src, data := range v.ConfigData {
			name := ObjectName(sim, v.Name, src)
			rename[src] = name
			objs = append(objs, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: cfg.Namespace, Name: name, Labels: labels(sim, v.Name)}, Data: data})
		}
		tmpl := Sanitize(v.Template, sim, v.Name, rename, profile, cfg)
		one := int32(1)
		name := ObjectName(sim, v.Name)
		objs = append(objs, &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Namespace: cfg.Namespace, Name: name, Labels: labels(sim, v.Name)},
			Spec:       appsv1.DeploymentSpec{Replicas: &one, Selector: &metav1.LabelSelector{MatchLabels: labels(sim, v.Name)}, Template: tmpl},
		})
		port := ContainerPort(v.Template)
		objs = append(objs, &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: cfg.Namespace, Name: name, Labels: labels(sim, v.Name)},
			Spec: corev1.ServiceSpec{Selector: labels(sim, v.Name),
				Ports: []corev1.ServicePort{{Name: "http", Port: port, TargetPort: intstr.FromInt32(port)}}},
		})
	}
	return objs
}

// ProbeJob builds the load-probe Job. Results are written to the container's
// termination message as JSON: {"baseline": {...}, "candidate": {...}}.
func ProbeJob(sim string, port int32, probe ProbeSpec, load v1.LoadProfile, cfg Config) *batchv1.Job {
	targets := []string{}
	for _, v := range []string{Baseline, Candidate} {
		targets = append(targets, fmt.Sprintf("%s=http://%s.%s.svc:%d", v, ObjectName(sim, v), cfg.Namespace, port))
	}
	args := []string{
		"--targets", strings.Join(targets, ","), "--path", probe.Path, "--method", probe.Method,
		"--rps", fmt.Sprint(load.RPS), "--duration", fmt.Sprint(load.DurationSeconds),
		"--concurrency", fmt.Sprint(load.Concurrency), "--output", "/dev/termination-log",
	}
	if probe.Body != "" {
		args = append(args, "--body", probe.Body)
	}
	zero := int32(0)
	ttl := int32(300)
	f := false
	nonRoot := true
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: cfg.Namespace, Name: ObjectName(sim, "probe"), Labels: labels(sim, "probe")},
		Spec: batchv1.JobSpec{
			BackoffLimit: &zero, TTLSecondsAfterFinished: &ttl,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels(sim, "probe")},
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyNever, ServiceAccountName: cfg.ServiceAccount, AutomountServiceAccountToken: &f,
					SecurityContext: &corev1.PodSecurityContext{RunAsNonRoot: &nonRoot, RunAsUser: ptr(int64(10001))},
					Containers: []corev1.Container{{
						Name: "probe", Image: cfg.ProbeImage, Command: cfg.ProbeCommand, Args: args,
						Env:                      []corev1.EnvVar{{Name: "OTEL_SDK_DISABLED", Value: "true"}},
						Resources:                resources("500m", "128Mi"),
						TerminationMessagePolicy: corev1.TerminationMessageReadFile,
						SecurityContext:          &corev1.SecurityContext{AllowPrivilegeEscalation: &f, ReadOnlyRootFilesystem: ptr(true)},
					}},
				},
			},
		},
	}
}

func ptr[T any](v T) *T { return &v }

// ProbeResult is the JSON document written by the probe.
type ProbeResult struct {
	Baseline  v1.SimulationMetrics `json:"baseline"`
	Candidate v1.SimulationMetrics `json:"candidate"`
}

// Verdict compares baseline and candidate deterministically.
func Verdict(b, c v1.SimulationMetrics) (string, string) {
	summary := fmt.Sprintf("error rate %.1f%% -> %.1f%%, p95 %.0fms -> %.0fms, memory growth %.1fMB -> %.1fMB",
		b.ErrorRate*100, c.ErrorRate*100, b.P95Ms, c.P95Ms, b.MemoryGrowthMB, c.MemoryGrowthMB)
	if b.Requests < 20 || c.Requests < 20 {
		return v1.VerdictInconclusive, "insufficient requests for a verdict; " + summary
	}
	errImproved := c.ErrorRate < b.ErrorRate*0.5 && b.ErrorRate-c.ErrorRate >= 0.02
	latImproved := b.P95Ms > 0 && c.P95Ms < b.P95Ms*0.7 && b.P95Ms-c.P95Ms >= 50
	memImproved := b.MemoryGrowthMB > 20 && c.MemoryGrowthMB < b.MemoryGrowthMB*0.5
	errRegressed := c.ErrorRate > b.ErrorRate+0.02 && c.ErrorRate > b.ErrorRate*1.5
	latRegressed := c.P95Ms > b.P95Ms*1.3 && c.P95Ms-b.P95Ms >= 50
	switch {
	case errRegressed || (latRegressed && !errImproved):
		return v1.VerdictRegressed, summary
	case errImproved || latImproved || memImproved:
		return v1.VerdictImproved, summary
	default:
		return v1.VerdictNoImprovement, summary
	}
}
