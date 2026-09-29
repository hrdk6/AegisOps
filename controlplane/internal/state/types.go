// Package state resolves observed cluster state from the controller's informer
// cache into compact, redacted views used by the policy engine and the
// read-only control-plane API.
package state

import "time"

// Annotation and label keys forming the workload metadata contract.
const (
	AnnotationTier              = "aegisops.io/tier"
	AnnotationDependencies      = "aegisops.io/dependencies"
	AnnotationOwner             = "aegisops.io/owner"
	AnnotationSimulationProfile = "aegisops.io/simulation-profile"
	AnnotationSimulationProbe   = "aegisops.io/simulation-probe"
	AnnotationRevision          = "deployment.kubernetes.io/revision"
	AnnotationRestartedAt       = "aegisops.io/restartedAt"
	LabelTrack                  = "track"
	LabelQuarantined            = "aegisops.io/quarantined"
)

// Container is a redacted container summary.
type Container struct {
	Name      string            `json:"name"`
	Image     string            `json:"image"`
	Env       map[string]string `json:"env"`
	Requests  map[string]string `json:"requests"`
	Limits    map[string]string `json:"limits"`
	ConfigMap []string          `json:"configMaps,omitempty"`
}

// Pod is a compact pod status view.
type Pod struct {
	Name                  string     `json:"name"`
	Phase                 string     `json:"phase"`
	Ready                 bool       `json:"ready"`
	Restarts              int32      `json:"restarts"`
	WaitingReason         string     `json:"waitingReason,omitempty"`
	LastTerminationReason string     `json:"lastTerminationReason,omitempty"`
	LastTerminationAt     *time.Time `json:"lastTerminationAt,omitempty"`
	LastExitCode          int32      `json:"lastExitCode,omitempty"`
	PodTemplateHash       string     `json:"podTemplateHash,omitempty"`
	Track                 string     `json:"track,omitempty"`
	Quarantined           bool       `json:"quarantined,omitempty"`
	CreatedAt             time.Time  `json:"createdAt"`
	Node                  string     `json:"node,omitempty"`
}

// RolloutRevision is one entry of the rollout history (a ReplicaSet).
type RolloutRevision struct {
	Revision        int64     `json:"revision"`
	ReplicaSet      string    `json:"replicaSet"`
	PodTemplateHash string    `json:"podTemplateHash"`
	CreatedAt       time.Time `json:"createdAt"`
	Images          []string  `json:"images"`
	Replicas        int32     `json:"replicas"`
	Version         string    `json:"version,omitempty"`
}

// Workload is the read model of a Deployment.
type Workload struct {
	Namespace          string            `json:"namespace"`
	Name               string            `json:"name"`
	Tier               string            `json:"tier"`
	Owner              string            `json:"owner,omitempty"`
	Dependencies       []string          `json:"dependencies"`
	Dependents         []string          `json:"dependents"`
	Replicas           int32             `json:"replicas"`
	ReadyReplicas      int32             `json:"readyReplicas"`
	AvailableReplicas  int32             `json:"availableReplicas"`
	UpdatedReplicas    int32             `json:"updatedReplicas"`
	Generation         int64             `json:"generation"`
	ObservedGeneration int64             `json:"observedGeneration"`
	Paused             bool              `json:"paused"`
	Revision           int64             `json:"revision"`
	RolloutComplete    bool              `json:"rolloutComplete"`
	RolloutMessage     string            `json:"rolloutMessage"`
	Containers         []Container       `json:"containers"`
	Pods               []Pod             `json:"pods"`
	Revisions          []RolloutRevision `json:"revisions"`
	Labels             map[string]string `json:"labels"`
	SimulationProfile  string            `json:"simulationProfile"`
	SimulationEnabled  bool              `json:"simulationEnabled"`
	CreatedAt          time.Time         `json:"createdAt"`
}

// Edge is a declared dependency.
type Edge struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// Topology is the declared dependency graph of a namespace.
type Topology struct {
	Namespace string            `json:"namespace"`
	Nodes     []TopologyNode    `json:"nodes"`
	Edges     []Edge            `json:"edges"`
	Tiers     map[string]string `json:"tiers"`
}

// TopologyNode is one workload in the graph.
type TopologyNode struct {
	Name     string `json:"name"`
	Tier     string `json:"tier"`
	Replicas int32  `json:"replicas"`
	Ready    int32  `json:"ready"`
}
