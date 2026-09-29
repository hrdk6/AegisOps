package v1alpha1

// RiskLevel is the deterministic risk classification assigned by the policy engine.
// +kubebuilder:validation:Enum=LOW;MEDIUM;HIGH;CRITICAL
type RiskLevel string

const (
	RiskLow      RiskLevel = "LOW"
	RiskMedium   RiskLevel = "MEDIUM"
	RiskHigh     RiskLevel = "HIGH"
	RiskCritical RiskLevel = "CRITICAL"
)

// Rank returns an ordinal usable for comparisons (LOW=1 ... CRITICAL=4, unknown=0).
func (r RiskLevel) Rank() int {
	switch r {
	case RiskLow:
		return 1
	case RiskMedium:
		return 2
	case RiskHigh:
		return 3
	case RiskCritical:
		return 4
	default:
		return 0
	}
}

// ActionType enumerates every action class the control plane knows about.
// Known-but-prohibited classes are listed so that attempts are recorded and
// explicitly denied rather than silently dropped.
// +kubebuilder:validation:Enum=restart_pod;rollout_restart;scale_deployment;rollback_deployment;patch_resources;pause_rollout;resume_rollout;update_config;isolate_pod;apply_network_policy;abort_canary;revert_action;delete_workload;delete_volume;modify_rbac;exec_command;schema_migration
type ActionType string

const (
	ActionRestartPod         ActionType = "restart_pod"
	ActionRolloutRestart     ActionType = "rollout_restart"
	ActionScaleDeployment    ActionType = "scale_deployment"
	ActionRollbackDeployment ActionType = "rollback_deployment"
	ActionPatchResources     ActionType = "patch_resources"
	ActionPauseRollout       ActionType = "pause_rollout"
	ActionResumeRollout      ActionType = "resume_rollout"
	ActionUpdateConfig       ActionType = "update_config"
	ActionIsolatePod         ActionType = "isolate_pod"
	ActionApplyNetworkPolicy ActionType = "apply_network_policy"
	ActionAbortCanary        ActionType = "abort_canary"
	ActionRevert             ActionType = "revert_action"

	// Prohibited action classes: always denied by policy.
	ActionDeleteWorkload  ActionType = "delete_workload"
	ActionDeleteVolume    ActionType = "delete_volume"
	ActionModifyRBAC      ActionType = "modify_rbac"
	ActionExecCommand     ActionType = "exec_command"
	ActionSchemaMigration ActionType = "schema_migration"
)

// TargetRef identifies the Kubernetes object an action operates on.
type TargetRef struct {
	// +kubebuilder:validation:Enum=Deployment;Pod;CanaryRelease
	Kind string `json:"kind"`
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	// +kubebuilder:validation:MaxLength=63
	Namespace string `json:"namespace"`
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-.a-z0-9]*[a-z0-9])?$`
	// +kubebuilder:validation:MaxLength=253
	Name string `json:"name"`
}

// Key returns a stable identifier for the target.
func (t TargetRef) Key() string { return t.Kind + "/" + t.Namespace + "/" + t.Name }

// ResourcePatch is a bounded set of container resource settings.
type ResourcePatch struct {
	CPURequest    string `json:"cpuRequest,omitempty"`
	CPULimit      string `json:"cpuLimit,omitempty"`
	MemoryRequest string `json:"memoryRequest,omitempty"`
	MemoryLimit   string `json:"memoryLimit,omitempty"`
}

// IsEmpty reports whether no field is set.
func (r *ResourcePatch) IsEmpty() bool {
	return r == nil || (r.CPURequest == "" && r.CPULimit == "" && r.MemoryRequest == "" && r.MemoryLimit == "")
}

// ActionParameters is a closed union of typed parameters. Which fields are
// permitted depends on the action type; the action registry rejects any field
// that the action does not declare. There is deliberately no free-form field:
// the schema cannot represent a shell command, a raw manifest or a kubeconfig.
type ActionParameters struct {
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=50
	Replicas *int32 `json:"replicas,omitempty"`
	// +kubebuilder:validation:Minimum=1
	ToRevision *int64 `json:"toRevision,omitempty"`
	// +kubebuilder:validation:MaxLength=63
	Container string         `json:"container,omitempty"`
	Resources *ResourcePatch `json:"resources,omitempty"`
	// +kubebuilder:validation:MaxLength=253
	ConfigMap string `json:"configMap,omitempty"`
	// +kubebuilder:validation:MaxLength=64
	ConfigRevision string `json:"configRevision,omitempty"`
	// +kubebuilder:validation:Enum=deny-all-ingress;allow-same-namespace-only
	NetworkPolicyTemplate string `json:"networkPolicyTemplate,omitempty"`
}
