package registry

import (
	"testing"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

func i32(v int32) *int32 { return &v }

func dep(name string) v1.TargetRef {
	return v1.TargetRef{Kind: "Deployment", Namespace: "shop", Name: name}
}

func TestValidateStructure(t *testing.T) {
	cases := []struct {
		name    string
		typ     v1.ActionType
		target  v1.TargetRef
		params  v1.ActionParameters
		revert  string
		wantErr bool
	}{
		{"scale ok", v1.ActionScaleDeployment, dep("payment"), v1.ActionParameters{Replicas: i32(3)}, "", false},
		{"scale missing replicas", v1.ActionScaleDeployment, dep("payment"), v1.ActionParameters{}, "", true},
		{"scale with undeclared param", v1.ActionScaleDeployment, dep("payment"),
			v1.ActionParameters{Replicas: i32(3), ConfigMap: "x"}, "", true},
		{"restart pod wrong kind", v1.ActionRestartPod, dep("payment"), v1.ActionParameters{}, "", true},
		{"unknown type", v1.ActionType("rm_rf"), dep("payment"), v1.ActionParameters{}, "", true},
		{"prohibited passes structure (policy denies)", v1.ActionExecCommand, dep("payment"), v1.ActionParameters{}, "", false},
		{"bad quantity", v1.ActionPatchResources, dep("payment"),
			v1.ActionParameters{Resources: &v1.ResourcePatch{CPULimit: "lots"}}, "", true},
		{"request above limit", v1.ActionPatchResources, dep("payment"),
			v1.ActionParameters{Resources: &v1.ResourcePatch{MemoryRequest: "512Mi", MemoryLimit: "256Mi"}}, "", true},
		{"resources ok", v1.ActionPatchResources, dep("payment"),
			v1.ActionParameters{Resources: &v1.ResourcePatch{CPULimit: "500m", MemoryLimit: "256Mi"}}, "", false},
		{"revert needs revertOf", v1.ActionRevert, dep("payment"), v1.ActionParameters{}, "", true},
		{"revertOf only on revert", v1.ActionRolloutRestart, dep("payment"), v1.ActionParameters{}, "act-1", true},
		{"update config needs configmap", v1.ActionUpdateConfig, dep("auth"), v1.ActionParameters{}, "", true},
		{"netpol template", v1.ActionApplyNetworkPolicy, dep("auth"),
			v1.ActionParameters{NetworkPolicyTemplate: "deny-all-ingress"}, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateStructure(tc.typ, tc.target, tc.params, tc.revert)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err=%v wantErr=%v", err, tc.wantErr)
			}
		})
	}
}

func TestRegistryHasTenExecutableActions(t *testing.T) {
	n := 0
	for _, s := range All() {
		if !s.Prohibited {
			n++
		}
	}
	if n < 10 {
		t.Fatalf("expected at least 10 executable actions, got %d", n)
	}
}
