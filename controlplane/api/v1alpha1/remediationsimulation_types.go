package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SimulationPhase is the lifecycle phase of a RemediationSimulation.
type SimulationPhase string

const (
	SimPending      SimulationPhase = "Pending"
	SimProvisioning SimulationPhase = "Provisioning"
	SimRunning      SimulationPhase = "Running"
	SimCompleted    SimulationPhase = "Completed"
	SimFailed       SimulationPhase = "Failed"
	// SimUnavailable means the proposal cannot be meaningfully simulated; the
	// caller must fall back to policy-only evaluation.
	SimUnavailable SimulationPhase = "Unavailable"
)

// Simulation verdicts (computed deterministically by the controller).
const (
	VerdictImproved      = "Improved"
	VerdictNoImprovement = "NoImprovement"
	VerdictRegressed     = "Regressed"
	VerdictInconclusive  = "Inconclusive"
)

// SimulationProposal is the change under test.
type SimulationProposal struct {
	ActionType ActionType `json:"actionType"`
	// +optional
	Parameters ActionParameters `json:"parameters,omitempty"`
}

// LoadProfile is the synthetic workload replayed against baseline and candidate.
// Path/Method/Body default to the target's aegisops.io/simulation-probe annotation.
type LoadProfile struct {
	// +kubebuilder:validation:Pattern=`^/[A-Za-z0-9/_\-.]*$`
	// +kubebuilder:validation:MaxLength=200
	// +optional
	Path string `json:"path,omitempty"`
	// +kubebuilder:validation:Enum=GET;POST
	// +optional
	Method string `json:"method,omitempty"`
	// +kubebuilder:validation:MaxLength=2000
	// +optional
	Body string `json:"body,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100
	RPS int32 `json:"rps"`
	// +kubebuilder:validation:Minimum=5
	// +kubebuilder:validation:Maximum=60
	DurationSeconds int32 `json:"durationSeconds"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=32
	Concurrency int32 `json:"concurrency"`
}

// RemediationSimulationSpec requests an isolated A/B test of a proposed change.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="spec is immutable"
type RemediationSimulationSpec struct {
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9-]{1,64}$`
	IncidentID  string             `json:"incidentId"`
	Target      TargetRef          `json:"target"`
	Proposal    SimulationProposal `json:"proposal"`
	Load        LoadProfile        `json:"load"`
	RequestedBy string             `json:"requestedBy"`
}

// SimulationMetrics are measured by the in-sandbox load probe.
type SimulationMetrics struct {
	Requests       int64   `json:"requests"`
	Errors         int64   `json:"errors"`
	ErrorRate      float64 `json:"errorRate"`
	P50Ms          float64 `json:"p50Ms"`
	P95Ms          float64 `json:"p95Ms"`
	P99Ms          float64 `json:"p99Ms"`
	ThroughputRPS  float64 `json:"throughputRps"`
	CPUMillicores  float64 `json:"cpuMillicores"`
	MemoryMB       float64 `json:"memoryMb"`
	MemoryGrowthMB float64 `json:"memoryGrowthMb"`
}

// RemediationSimulationStatus is owned by the controller.
type RemediationSimulationStatus struct {
	// +optional
	Phase SimulationPhase `json:"phase,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Verdict string `json:"verdict,omitempty"`
	// +optional
	Summary string `json:"summary,omitempty"`
	// +optional
	Baseline *SimulationMetrics `json:"baseline,omitempty"`
	// +optional
	Candidate *SimulationMetrics `json:"candidate,omitempty"`
	// ProposalHash binds this result to (target, actionType, parameters).
	// +optional
	ProposalHash string `json:"proposalHash,omitempty"`
	// +optional
	SandboxNamespace string `json:"sandboxNamespace,omitempty"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=rsim,categories=aegisops
// +kubebuilder:printcolumn:name="Incident",type=string,JSONPath=`.spec.incidentId`
// +kubebuilder:printcolumn:name="Target",type=string,JSONPath=`.spec.target.name`
// +kubebuilder:printcolumn:name="Proposal",type=string,JSONPath=`.spec.proposal.actionType`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Verdict",type=string,JSONPath=`.status.verdict`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// RemediationSimulation is an isolated digital-twin test of a proposed remediation.
type RemediationSimulation struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   RemediationSimulationSpec   `json:"spec"`
	Status RemediationSimulationStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// RemediationSimulationList contains a list of RemediationSimulation.
type RemediationSimulationList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RemediationSimulation `json:"items"`
}

func init() {
	SchemeBuilder.Register(&RemediationSimulation{}, &RemediationSimulationList{})
}
