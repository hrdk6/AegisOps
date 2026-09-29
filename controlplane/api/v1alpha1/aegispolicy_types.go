package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Automation modes.
const (
	ModeAutonomous = "autonomous"
	ModeSupervised = "supervised"
	ModeObserve    = "observe"
)

// ProtectedWorkload marks a workload that automation must not touch freely.
type ProtectedWorkload struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	// +kubebuilder:validation:Enum=deny;approval
	Mode string `json:"mode"`
}

// PolicyLimits bound the parameters of otherwise-permitted actions.
type PolicyLimits struct {
	// +kubebuilder:validation:Minimum=0
	MinReplicas int32 `json:"minReplicas"`
	// +kubebuilder:validation:Maximum=50
	MaxReplicas int32 `json:"maxReplicas"`
	// Maximum multiplicative scale-up in a single action (e.g. 3.0).
	MaxScaleUpFactor float64 `json:"maxScaleUpFactor"`
	MaxCPU           string  `json:"maxCpu"`
	MaxMemory        string  `json:"maxMemory"`
	// Maximum percent of a workload's pods disrupted by one action.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100
	MaxDisruptionPercent int32 `json:"maxDisruptionPercent"`
}

// CircuitBreakerSpec configures the global remediation circuit breaker.
type CircuitBreakerSpec struct {
	FailureThreshold int32 `json:"failureThreshold"`
	WindowSeconds    int32 `json:"windowSeconds"`
	OpenSeconds      int32 `json:"openSeconds"`
}

// PolicyBudgets bound autonomous behaviour to prevent runaway loops.
type PolicyBudgets struct {
	MaxActionsPerIncident     int32              `json:"maxActionsPerIncident"`
	MaxRevertsPerIncident     int32              `json:"maxRevertsPerIncident"`
	TargetCooldownSeconds     int32              `json:"targetCooldownSeconds"`
	GlobalMaxActionsPerWindow int32              `json:"globalMaxActionsPerWindow"`
	GlobalWindowSeconds       int32              `json:"globalWindowSeconds"`
	CircuitBreaker            CircuitBreakerSpec `json:"circuitBreaker"`
}

// ActionRule overrides registry defaults for one action type.
type ActionRule struct {
	ActionType ActionType `json:"actionType"`
	// +optional
	Enabled *bool `json:"enabled,omitempty"`
	// +optional
	MinRisk RiskLevel `json:"minRisk,omitempty"`
	// +optional
	RequireApproval bool `json:"requireApproval,omitempty"`
}

// AegisPolicySpec is the operator-controlled safety envelope.
type AegisPolicySpec struct {
	// +kubebuilder:validation:Enum=autonomous;supervised;observe
	Mode              string   `json:"mode"`
	AllowedNamespaces []string `json:"allowedNamespaces"`
	// +optional
	ProtectedWorkloads []ProtectedWorkload `json:"protectedWorkloads,omitempty"`
	// Actions below this diagnosis confidence (0-100) require approval.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=100
	AutonomyMinConfidence int32 `json:"autonomyMinConfidence"`
	// Risk at or above this level requires approval (unless simulation-gated).
	ApprovalRiskThreshold RiskLevel `json:"approvalRiskThreshold"`
	// When true, a MEDIUM-risk action whose simulation verdict is Improved may run without approval.
	AllowSimulationAutoApproval bool          `json:"allowSimulationAutoApproval"`
	Limits                      PolicyLimits  `json:"limits"`
	Budgets                     PolicyBudgets `json:"budgets"`
	// +optional
	ActionRules []ActionRule `json:"actionRules,omitempty"`
	// +kubebuilder:validation:Minimum=30
	ApprovalTTLSeconds int32 `json:"approvalTtlSeconds"`
	// +kubebuilder:validation:Minimum=10
	ExecutionTimeoutSeconds int32 `json:"executionTimeoutSeconds"`
	// AutoRevertOnFailure restores the snapshot when execution fails or times out.
	AutoRevertOnFailure bool `json:"autoRevertOnFailure"`
}

// CircuitBreakerStatus reports the derived breaker state.
type CircuitBreakerStatus struct {
	State          string       `json:"state"`
	RecentFailures int32        `json:"recentFailures"`
	OpenedAt       *metav1.Time `json:"openedAt,omitempty"`
	Reason         string       `json:"reason,omitempty"`
}

// AegisPolicyStatus is owned by the controller.
type AegisPolicyStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	CircuitBreaker CircuitBreakerStatus `json:"circuitBreaker,omitempty"`
	// +optional
	LastUpdated *metav1.Time `json:"lastUpdated,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=apol,categories=aegisops
// +kubebuilder:printcolumn:name="Mode",type=string,JSONPath=`.spec.mode`
// +kubebuilder:printcolumn:name="Breaker",type=string,JSONPath=`.status.circuitBreaker.state`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// AegisPolicy is the cluster-wide safety policy consulted for every action.
type AegisPolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AegisPolicySpec   `json:"spec"`
	Status AegisPolicyStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AegisPolicyList contains a list of AegisPolicy.
type AegisPolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AegisPolicy `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AegisPolicy{}, &AegisPolicyList{})
}

// DefaultPolicySpec is used when no AegisPolicy object exists. It is
// deliberately conservative: supervised mode, nothing executes without approval.
func DefaultPolicySpec() AegisPolicySpec {
	return AegisPolicySpec{
		Mode:                        ModeSupervised,
		AllowedNamespaces:           []string{},
		AutonomyMinConfidence:       70,
		ApprovalRiskThreshold:       RiskMedium,
		AllowSimulationAutoApproval: false,
		Limits: PolicyLimits{
			MinReplicas: 1, MaxReplicas: 6, MaxScaleUpFactor: 2.0,
			MaxCPU: "1", MaxMemory: "1Gi", MaxDisruptionPercent: 50,
		},
		Budgets: PolicyBudgets{
			MaxActionsPerIncident: 3, MaxRevertsPerIncident: 1, TargetCooldownSeconds: 180,
			GlobalMaxActionsPerWindow: 5, GlobalWindowSeconds: 600,
			CircuitBreaker: CircuitBreakerSpec{FailureThreshold: 2, WindowSeconds: 900, OpenSeconds: 900},
		},
		ApprovalTTLSeconds:      900,
		ExecutionTimeoutSeconds: 180,
		AutoRevertOnFailure:     true,
	}
}
