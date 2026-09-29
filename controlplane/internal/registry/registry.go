// Package registry is the explicit allowlist of actions the control plane can
// perform. Anything not described here cannot be executed, regardless of what
// an upstream caller (including the AI layer) asks for.
package registry

import (
	"fmt"
	"sort"

	"k8s.io/apimachinery/pkg/api/resource"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

// Param names used for allow/require lists.
const (
	ParamReplicas              = "replicas"
	ParamToRevision            = "toRevision"
	ParamContainer             = "container"
	ParamResources             = "resources"
	ParamConfigMap             = "configMap"
	ParamConfigRevision        = "configRevision"
	ParamNetworkPolicyTemplate = "networkPolicyTemplate"
)

// Spec describes one action class.
type Spec struct {
	Type        v1.ActionType `json:"type"`
	Description string        `json:"description"`
	// BaseRisk is the starting risk score (0-100) before contextual modifiers.
	BaseRisk int32 `json:"baseRisk"`
	// TargetKinds lists the object kinds this action may target.
	TargetKinds []string `json:"targetKinds"`
	Allowed     []string `json:"allowedParams"`
	Required    []string `json:"requiredParams"`
	// Reversible means the controller can snapshot and later restore state.
	Reversible bool `json:"reversible"`
	// Simulatable means a sandbox A/B simulation is meaningful for this action.
	Simulatable bool `json:"simulatable"`
	// Disruptive means the action terminates or replaces running pods.
	Disruptive bool `json:"disruptive"`
	// Prohibited action classes are always denied.
	Prohibited bool `json:"prohibited"`
}

var specs = map[v1.ActionType]Spec{
	v1.ActionRestartPod: {
		Type: v1.ActionRestartPod, Description: "Delete a single pod so its ReplicaSet replaces it.",
		BaseRisk: 10, TargetKinds: []string{"Pod"}, Disruptive: true,
	},
	v1.ActionRolloutRestart: {
		Type: v1.ActionRolloutRestart, Description: "Rolling restart of every pod in a Deployment.",
		BaseRisk: 25, TargetKinds: []string{"Deployment"}, Disruptive: true,
	},
	v1.ActionScaleDeployment: {
		Type: v1.ActionScaleDeployment, Description: "Change a Deployment's replica count within policy limits.",
		BaseRisk: 15, TargetKinds: []string{"Deployment"}, Allowed: []string{ParamReplicas},
		Required: []string{ParamReplicas}, Reversible: true,
	},
	v1.ActionRollbackDeployment: {
		Type: v1.ActionRollbackDeployment, Description: "Restore a previous ReplicaSet pod template (rollout undo).",
		BaseRisk: 30, TargetKinds: []string{"Deployment"}, Allowed: []string{ParamToRevision},
		Reversible: true, Simulatable: true, Disruptive: true,
	},
	v1.ActionPatchResources: {
		Type: v1.ActionPatchResources, Description: "Set CPU/memory requests and limits of one container.",
		BaseRisk: 32, TargetKinds: []string{"Deployment"}, Allowed: []string{ParamContainer, ParamResources},
		Required: []string{ParamResources}, Reversible: true, Simulatable: true, Disruptive: true,
	},
	v1.ActionPauseRollout: {
		Type: v1.ActionPauseRollout, Description: "Pause an in-progress Deployment rollout or CanaryRelease.",
		BaseRisk: 15, TargetKinds: []string{"Deployment", "CanaryRelease"}, Reversible: true,
	},
	v1.ActionResumeRollout: {
		Type: v1.ActionResumeRollout, Description: "Resume a paused Deployment rollout or CanaryRelease.",
		BaseRisk: 25, TargetKinds: []string{"Deployment", "CanaryRelease"}, Reversible: true,
	},
	v1.ActionUpdateConfig: {
		Type: v1.ActionUpdateConfig, Description: "Restore a recorded revision of a ConfigMap consumed by the Deployment, then roll it.",
		BaseRisk: 35, TargetKinds: []string{"Deployment"}, Allowed: []string{ParamConfigMap, ParamConfigRevision},
		Required: []string{ParamConfigMap}, Reversible: true, Simulatable: true, Disruptive: true,
	},
	v1.ActionIsolatePod: {
		Type: v1.ActionIsolatePod, Description: "Detach a pod from its Service and ReplicaSet (kept for forensics).",
		BaseRisk: 30, TargetKinds: []string{"Pod"}, Reversible: true,
	},
	v1.ActionApplyNetworkPolicy: {
		Type: v1.ActionApplyNetworkPolicy, Description: "Apply a predefined NetworkPolicy template to a workload.",
		BaseRisk: 65, TargetKinds: []string{"Deployment"}, Allowed: []string{ParamNetworkPolicyTemplate},
		Required: []string{ParamNetworkPolicyTemplate}, Reversible: true,
	},
	v1.ActionAbortCanary: {
		Type: v1.ActionAbortCanary, Description: "Abort a CanaryRelease and return all traffic to stable.",
		BaseRisk: 10, TargetKinds: []string{"CanaryRelease"},
	},
	v1.ActionRevert: {
		Type: v1.ActionRevert, Description: "Restore the pre-action snapshot of a previous action.",
		BaseRisk: 25, TargetKinds: []string{"Deployment", "Pod", "CanaryRelease"}, Disruptive: true,
	},
	v1.ActionDeleteWorkload: {Type: v1.ActionDeleteWorkload, Description: "Delete a workload.", BaseRisk: 100, Prohibited: true},
	v1.ActionDeleteVolume:   {Type: v1.ActionDeleteVolume, Description: "Delete a persistent volume.", BaseRisk: 100, Prohibited: true},
	v1.ActionModifyRBAC:     {Type: v1.ActionModifyRBAC, Description: "Change RBAC/IAM permissions.", BaseRisk: 100, Prohibited: true},
	v1.ActionExecCommand:    {Type: v1.ActionExecCommand, Description: "Execute an arbitrary command in a container.", BaseRisk: 100, Prohibited: true},
	v1.ActionSchemaMigration: {
		Type: v1.ActionSchemaMigration, Description: "Run a database schema change.", BaseRisk: 100, Prohibited: true,
	},
}

// Lookup returns the spec for an action type.
func Lookup(t v1.ActionType) (Spec, bool) {
	s, ok := specs[t]
	return s, ok
}

// All returns every registered spec, sorted by type.
func All() []Spec {
	out := make([]Spec, 0, len(specs))
	for _, s := range specs {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Type < out[j].Type })
	return out
}

// presentParams lists which parameter fields are set.
func presentParams(p v1.ActionParameters) []string {
	var out []string
	if p.Replicas != nil {
		out = append(out, ParamReplicas)
	}
	if p.ToRevision != nil {
		out = append(out, ParamToRevision)
	}
	if p.Container != "" {
		out = append(out, ParamContainer)
	}
	if !p.Resources.IsEmpty() {
		out = append(out, ParamResources)
	}
	if p.ConfigMap != "" {
		out = append(out, ParamConfigMap)
	}
	if p.ConfigRevision != "" {
		out = append(out, ParamConfigRevision)
	}
	if p.NetworkPolicyTemplate != "" {
		out = append(out, ParamNetworkPolicyTemplate)
	}
	return out
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// ValidateStructure checks the action against its registry spec: known type,
// valid target kind, no undeclared parameters, all required parameters, and
// well-formed values. It does not consult cluster state or policy.
func ValidateStructure(t v1.ActionType, target v1.TargetRef, p v1.ActionParameters, revertOf string) error {
	s, ok := Lookup(t)
	if !ok {
		return fmt.Errorf("unknown action type %q", t)
	}
	if s.Prohibited {
		// Structure is irrelevant; the policy engine denies prohibited classes.
		return nil
	}
	if !contains(s.TargetKinds, target.Kind) {
		return fmt.Errorf("action %s cannot target kind %q (allowed: %v)", t, target.Kind, s.TargetKinds)
	}
	if target.Namespace == "" || target.Name == "" {
		return fmt.Errorf("target namespace and name are required")
	}
	present := presentParams(p)
	for _, name := range present {
		if !contains(s.Allowed, name) {
			return fmt.Errorf("parameter %q is not permitted for action %s", name, t)
		}
	}
	for _, name := range s.Required {
		if !contains(present, name) {
			return fmt.Errorf("parameter %q is required for action %s", name, t)
		}
	}
	if t == v1.ActionRevert && revertOf == "" {
		return fmt.Errorf("revert_action requires revertOf")
	}
	if t != v1.ActionRevert && revertOf != "" {
		return fmt.Errorf("revertOf is only valid for revert_action")
	}
	if p.Replicas != nil && (*p.Replicas < 0 || *p.Replicas > 50) {
		return fmt.Errorf("replicas out of range")
	}
	if p.ToRevision != nil && *p.ToRevision < 1 {
		return fmt.Errorf("toRevision must be >= 1")
	}
	if p.Resources != nil {
		for field, q := range map[string]string{
			"cpuRequest": p.Resources.CPURequest, "cpuLimit": p.Resources.CPULimit,
			"memoryRequest": p.Resources.MemoryRequest, "memoryLimit": p.Resources.MemoryLimit,
		} {
			if q == "" {
				continue
			}
			qty, err := resource.ParseQuantity(q)
			if err != nil {
				return fmt.Errorf("resources.%s: invalid quantity %q", field, q)
			}
			if qty.Sign() <= 0 {
				return fmt.Errorf("resources.%s must be positive", field)
			}
		}
		if err := checkRequestNotAboveLimit(p.Resources.CPURequest, p.Resources.CPULimit); err != nil {
			return fmt.Errorf("cpu: %w", err)
		}
		if err := checkRequestNotAboveLimit(p.Resources.MemoryRequest, p.Resources.MemoryLimit); err != nil {
			return fmt.Errorf("memory: %w", err)
		}
	}
	return nil
}

func checkRequestNotAboveLimit(req, limit string) error {
	if req == "" || limit == "" {
		return nil
	}
	r := resource.MustParse(req)
	l := resource.MustParse(limit)
	if r.Cmp(l) > 0 {
		return fmt.Errorf("request %s exceeds limit %s", req, limit)
	}
	return nil
}
