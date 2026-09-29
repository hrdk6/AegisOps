package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// CanaryPhase is the lifecycle phase of a CanaryRelease.
type CanaryPhase string

const (
	CanaryPending     CanaryPhase = "Pending"
	CanaryProgressing CanaryPhase = "Progressing"
	CanaryPaused      CanaryPhase = "Paused"
	CanaryPromoting   CanaryPhase = "Promoting"
	CanaryPromoted    CanaryPhase = "Promoted"
	CanaryAborted     CanaryPhase = "Aborted"
	CanaryFailed      CanaryPhase = "Failed"
)

// CanaryEnvVar is a literal environment variable (no valueFrom: releases cannot
// reference Secrets).
type CanaryEnvVar struct {
	// +kubebuilder:validation:Pattern=`^[A-Z_][A-Z0-9_]*$`
	Name string `json:"name"`
	// +kubebuilder:validation:MaxLength=1024
	Value string `json:"value"`
}

// CanaryTemplate is the bounded set of changes a release may make.
type CanaryTemplate struct {
	// +kubebuilder:validation:MaxLength=255
	Image string `json:"image"`
	// +kubebuilder:validation:MaxLength=64
	Version string `json:"version"`
	// +optional
	// +kubebuilder:validation:MaxItems=32
	Env []CanaryEnvVar `json:"env,omitempty"`
}

// CanaryStep shifts traffic weight and holds it for analysis.
type CanaryStep struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100
	Weight int32 `json:"weight"`
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:validation:Maximum=3600
	PauseSeconds int32 `json:"pauseSeconds"`
}

// CanaryAnalysis holds the deterministic SLO gates evaluated after each step.
type CanaryAnalysis struct {
	// Maximum tolerated canary 5xx ratio (0-1).
	MaxErrorRate float64 `json:"maxErrorRate"`
	// Maximum tolerated canary p95 latency in milliseconds.
	MaxP95Ms float64 `json:"maxP95Ms"`
	// Minimum canary requests in the window for a verdict to count.
	// +kubebuilder:validation:Minimum=1
	MinRequests int32 `json:"minRequests"`
}

// CanaryReleaseSpec describes a progressive rollout of a Deployment.
type CanaryReleaseSpec struct {
	// TargetRef is the name of the stable Deployment in the same namespace.
	TargetRef string         `json:"targetRef"`
	Release   CanaryTemplate `json:"release"`
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=10
	Steps    []CanaryStep   `json:"steps"`
	Analysis CanaryAnalysis `json:"analysis"`
	// TotalReplicas is the replica budget shared by stable and canary. Traffic
	// weight is approximated by replica ratio (no L7 traffic router).
	// +kubebuilder:validation:Minimum=2
	// +kubebuilder:validation:Maximum=20
	// +optional
	TotalReplicas int32 `json:"totalReplicas,omitempty"`
	// Paused halts progression (set by the pause_rollout action).
	// +optional
	Paused bool `json:"paused,omitempty"`
	// Abort returns all traffic to stable (set by the abort_canary action).
	// +optional
	Abort bool `json:"abort,omitempty"`
}

// CanaryAnalysisResult records one analysis evaluation.
type CanaryAnalysisResult struct {
	Step            int32       `json:"step"`
	Weight          int32       `json:"weight"`
	CanaryRequests  float64     `json:"canaryRequests"`
	CanaryErrorRate float64     `json:"canaryErrorRate"`
	StableErrorRate float64     `json:"stableErrorRate"`
	CanaryP95Ms     float64     `json:"canaryP95Ms"`
	StableP95Ms     float64     `json:"stableP95Ms"`
	Verdict         string      `json:"verdict"`
	Reason          string      `json:"reason,omitempty"`
	Time            metav1.Time `json:"time"`
}

// CanaryReleaseStatus is owned by the controller.
type CanaryReleaseStatus struct {
	// +optional
	Phase CanaryPhase `json:"phase,omitempty"`
	// +optional
	CurrentStep int32 `json:"currentStep"`
	// +optional
	StepStartedAt *metav1.Time `json:"stepStartedAt,omitempty"`
	// +optional
	CanaryReplicas int32 `json:"canaryReplicas"`
	// +optional
	StableReplicas int32 `json:"stableReplicas"`
	// +optional
	EffectiveWeight int32 `json:"effectiveWeight"`
	// +optional
	OriginalReplicas int32 `json:"originalReplicas,omitempty"`
	// +optional
	Analyses []CanaryAnalysisResult `json:"analyses,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=canary,categories=aegisops
// +kubebuilder:printcolumn:name="Target",type=string,JSONPath=`.spec.targetRef`
// +kubebuilder:printcolumn:name="Version",type=string,JSONPath=`.spec.release.version`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Weight",type=integer,JSONPath=`.status.effectiveWeight`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// CanaryRelease progressively shifts traffic to a new release with SLO gates.
type CanaryRelease struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   CanaryReleaseSpec   `json:"spec"`
	Status CanaryReleaseStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// CanaryReleaseList contains a list of CanaryRelease.
type CanaryReleaseList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []CanaryRelease `json:"items"`
}

func init() {
	SchemeBuilder.Register(&CanaryRelease{}, &CanaryReleaseList{})
}
