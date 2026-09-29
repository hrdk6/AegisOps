package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ActionPhase is the lifecycle phase of a RemediationAction.
type ActionPhase string

const (
	PhasePending          ActionPhase = "Pending"
	PhaseDenied           ActionPhase = "Denied"
	PhaseAwaitingApproval ActionPhase = "AwaitingApproval"
	PhaseApproved         ActionPhase = "Approved"
	PhaseRejected         ActionPhase = "Rejected"
	PhaseExpired          ActionPhase = "Expired"
	PhaseExecuting        ActionPhase = "Executing"
	PhaseSucceeded        ActionPhase = "Succeeded"
	PhaseFailed           ActionPhase = "Failed"
	PhaseRolledBack       ActionPhase = "RolledBack"
)

// IsTerminal reports whether no further transitions are expected.
// Succeeded is terminal for execution but may later transition to RolledBack
// when a revert_action restores the pre-action snapshot.
func (p ActionPhase) IsTerminal() bool {
	switch p {
	case PhaseDenied, PhaseRejected, PhaseExpired, PhaseSucceeded, PhaseFailed, PhaseRolledBack:
		return true
	}
	return false
}

// RemediationActionSpec is an immutable request to perform exactly one action.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="spec is immutable"
type RemediationActionSpec struct {
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9-]{1,64}$`
	IncidentID string     `json:"incidentId"`
	ActionType ActionType `json:"actionType"`
	Target     TargetRef  `json:"target"`
	// +optional
	Parameters ActionParameters `json:"parameters,omitempty"`
	// Justification is informational only. It is never interpreted by the controller.
	// +kubebuilder:validation:MaxLength=2000
	// +optional
	Justification string `json:"justification,omitempty"`
	// DiagnosisConfidence (0-100) as claimed by the requester. It can only make
	// the policy *more* conservative; it never grants permissions.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=100
	// +optional
	DiagnosisConfidence int32 `json:"diagnosisConfidence,omitempty"`
	// SimulationRef names a RemediationSimulation whose verdict the policy may consult.
	// +optional
	SimulationRef string `json:"simulationRef,omitempty"`
	// RevertOf names the action whose pre-action snapshot a revert_action restores.
	// +optional
	RevertOf string `json:"revertOf,omitempty"`
	// Preconditions pin the target state the proposal was planned against. If the
	// target changed since planning (for example a new rollout), the action is
	// denied at admission or fails at execution instead of acting on stale analysis.
	// +optional
	Preconditions *ActionPreconditions `json:"preconditions,omitempty"`
	// RequestedBy is stamped by the control-plane API from the authenticated identity.
	RequestedBy string `json:"requestedBy"`
}

// ActionPreconditions describe the target state an action was planned against.
type ActionPreconditions struct {
	// ExpectedRevision is the Deployment rollout revision observed at planning time.
	// +kubebuilder:validation:Minimum=1
	// +optional
	ExpectedRevision *int64 `json:"expectedRevision,omitempty"`
}

// PolicyCheck records the outcome of a single deterministic policy rule.
type PolicyCheck struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail,omitempty"`
}

// PolicyDecision is the output of the deterministic policy engine.
type PolicyDecision struct {
	Allowed          bool          `json:"allowed"`
	RequiresApproval bool          `json:"requiresApproval"`
	RiskLevel        RiskLevel     `json:"riskLevel"`
	RiskScore        int32         `json:"riskScore"`
	Reasons          []string      `json:"reasons,omitempty"`
	Checks           []PolicyCheck `json:"checks,omitempty"`
	Mode             string        `json:"mode,omitempty"`
	PolicyGeneration int64         `json:"policyGeneration,omitempty"`
	EvaluatedAt      metav1.Time   `json:"evaluatedAt"`
}

// ApprovalRecord is written only after an HMAC-verified human decision.
type ApprovalRecord struct {
	// +kubebuilder:validation:Enum=approved;rejected
	Decision          string      `json:"decision"`
	Approver          string      `json:"approver"`
	Reason            string      `json:"reason,omitempty"`
	DecidedAt         metav1.Time `json:"decidedAt"`
	SignatureVerified bool        `json:"signatureVerified"`
}

// Snapshot captures the pre-action state needed to revert an action.
type Snapshot struct {
	Kind       string      `json:"kind"`
	Reversible bool        `json:"reversible"`
	Data       string      `json:"data,omitempty"`
	TakenAt    metav1.Time `json:"takenAt"`
}

// RemediationActionStatus is owned exclusively by the controller.
type RemediationActionStatus struct {
	// +optional
	Phase ActionPhase `json:"phase,omitempty"`
	// +optional
	Decision *PolicyDecision `json:"decision,omitempty"`
	// +optional
	Approval *ApprovalRecord `json:"approval,omitempty"`
	// +optional
	Snapshot *Snapshot `json:"snapshot,omitempty"`
	// SpecHash binds approvals and simulations to this exact spec.
	// +optional
	SpecHash string `json:"specHash,omitempty"`
	// Applied is set once the mutation has been sent to the API server. An
	// Executing action with Applied=false is re-applied after a controller restart.
	// +optional
	Applied bool `json:"applied,omitempty"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	RevertedBy string `json:"revertedBy,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=ract,categories=aegisops
// +kubebuilder:printcolumn:name="Incident",type=string,JSONPath=`.spec.incidentId`
// +kubebuilder:printcolumn:name="Action",type=string,JSONPath=`.spec.actionType`
// +kubebuilder:printcolumn:name="Target",type=string,JSONPath=`.spec.target.name`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Risk",type=string,JSONPath=`.status.decision.riskLevel`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// RemediationAction is a single, policy-gated change requested against the cluster.
type RemediationAction struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   RemediationActionSpec   `json:"spec"`
	Status RemediationActionStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// RemediationActionList contains a list of RemediationAction.
type RemediationActionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RemediationAction `json:"items"`
}

func init() {
	SchemeBuilder.Register(&RemediationAction{}, &RemediationActionList{})
}
