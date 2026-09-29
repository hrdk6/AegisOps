package sandbox

import (
	"testing"

	corev1 "k8s.io/api/core/v1"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

func TestSanitizeStripsSecretsAndIdentity(t *testing.T) {
	tmpl := corev1.PodTemplateSpec{Spec: corev1.PodSpec{
		ServiceAccountName: "payment-sa",
		Volumes: []corev1.Volume{
			{Name: "creds", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "db"}}},
			{Name: "cfg", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: "payment-config"}}}},
			{Name: "data", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "pvc"}}},
		},
		Containers: []corev1.Container{{
			Name: "app",
			Env: []corev1.EnvVar{
				{Name: "DB_PASSWORD", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{Key: "pw"}}},
				{Name: "FEATURE", Value: "on"},
			},
			EnvFrom:      []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{}}, {ConfigMapRef: &corev1.ConfigMapEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "payment-config"}}}},
			VolumeMounts: []corev1.VolumeMount{{Name: "creds", MountPath: "/secrets"}, {Name: "cfg", MountPath: "/cfg"}},
		}},
	}}
	cfg := Config{Namespace: "aegis-sandbox", ServiceAccount: "aegis-sandbox-runner", PostgresImage: "pg", RedisImage: "redis"}
	out := Sanitize(tmpl, "sim-1", Candidate, map[string]string{"payment-config": "sim-1-candidate-payment-config"}, []string{"postgres", "redis"}, cfg)

	if out.Spec.ServiceAccountName != "aegis-sandbox-runner" || out.Spec.AutomountServiceAccountToken == nil || *out.Spec.AutomountServiceAccountToken {
		t.Fatal("service account not replaced / token automount not disabled")
	}
	for _, v := range out.Spec.Volumes {
		if v.Secret != nil {
			t.Fatal("secret volume leaked into sandbox")
		}
		if v.PersistentVolumeClaim != nil {
			t.Fatal("pvc leaked into sandbox")
		}
		if v.ConfigMap != nil && v.ConfigMap.Name != "sim-1-candidate-payment-config" {
			t.Fatalf("configmap not renamed: %s", v.ConfigMap.Name)
		}
	}
	app := out.Spec.Containers[0]
	for _, e := range app.Env {
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			t.Fatal("secret env leaked")
		}
	}
	for _, ef := range app.EnvFrom {
		if ef.SecretRef != nil {
			t.Fatal("secret envFrom leaked")
		}
	}
	for _, m := range app.VolumeMounts {
		if m.Name == "creds" {
			t.Fatal("mount of dropped secret volume kept")
		}
	}
	if len(out.Spec.Containers) != 3 {
		t.Fatalf("expected app + 2 sidecars, got %d", len(out.Spec.Containers))
	}
	found := false
	for _, e := range app.Env {
		if e.Name == "AEGIS_SANDBOX" && e.Value == "1" {
			found = true
		}
	}
	if !found {
		t.Fatal("AEGIS_SANDBOX not set")
	}
}

func TestVerdict(t *testing.T) {
	bad := v1.SimulationMetrics{Requests: 200, ErrorRate: 0.34, P95Ms: 800}
	good := v1.SimulationMetrics{Requests: 200, ErrorRate: 0.0, P95Ms: 40}
	if v, _ := Verdict(bad, good); v != v1.VerdictImproved {
		t.Fatalf("expected Improved, got %s", v)
	}
	if v, _ := Verdict(good, bad); v != v1.VerdictRegressed {
		t.Fatalf("expected Regressed, got %s", v)
	}
	if v, _ := Verdict(good, good); v != v1.VerdictNoImprovement {
		t.Fatalf("expected NoImprovement, got %s", v)
	}
	if v, _ := Verdict(v1.SimulationMetrics{Requests: 3}, good); v != v1.VerdictInconclusive {
		t.Fatalf("expected Inconclusive, got %s", v)
	}
	leak := v1.SimulationMetrics{Requests: 200, P95Ms: 50, MemoryGrowthMB: 60}
	fixed := v1.SimulationMetrics{Requests: 200, P95Ms: 50, MemoryGrowthMB: 2}
	if v, _ := Verdict(leak, fixed); v != v1.VerdictImproved {
		t.Fatalf("expected memory improvement, got %s", v)
	}
}

func TestObjectNameBounded(t *testing.T) {
	n := ObjectName("sim-inc-20260101-abcdef-very-long-name", "candidate", "a-rather-long-configmap-name-for-testing")
	if len(n) > 63 {
		t.Fatalf("name too long: %d", len(n))
	}
}

func TestParseProbe(t *testing.T) {
	if _, err := ParseProbe(`{"path":"/pay","method":"POST"}`); err != nil {
		t.Fatal(err)
	}
	if _, err := ParseProbe(`{"path":"pay","method":"DELETE"}`); err == nil {
		t.Fatal("invalid probe accepted")
	}
}
