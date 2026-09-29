// Package policy is the deterministic safety gate between remediation proposals
// and execution. It is a pure function of its inputs: no I/O, no clocks other
// than Input.Now, no model output other than the (untrusted, only-restrictive)
// confidence claim.
package policy

import (
	"time"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

// Workload tiers (from the aegisops.io/tier annotation).
const (
	TierCritical       = "critical"
	TierStandard       = "standard"
	TierStateful       = "stateful"
	TierInfrastructure = "infrastructure"
)

// TargetState is the observed state of the action's target, resolved by the
// controller from its informer cache (never supplied by the requester).
type TargetState struct {
	Exists     bool
	Kind       string
	Namespace  string
	Name       string
	Deployment string // owning Deployment (for Pod targets) or the Deployment itself
	Replicas   int32
	Ready      int32
	Tier       string
	Dependents []string
	// Revisions lists ReplicaSet revisions available for rollback.
	Revisions       []int64
	CurrentRevision int64
	ConfigMaps      []string
	// ConfigRevisionAvailable is true when a recorded ConfigMap revision exists to restore.
	ConfigRevisionAvailable bool
	Containers              []string
	Paused                  bool
	RolloutInProgress       bool
	CanaryPhase             v1.CanaryPhase
}

// ActionRecord is a compact view of an existing RemediationAction.
type ActionRecord struct {
	Name        string
	IncidentID  string
	Type        v1.ActionType
	TargetKey   string
	ParamsHash  string
	Phase       v1.ActionPhase
	CreatedAt   time.Time
	CompletedAt *time.Time
	RevertOf    string
	Reversible  bool
	RevertedBy  string
}

// SimulationState is the observed state of a referenced simulation.
type SimulationState struct {
	Name         string
	Phase        v1.SimulationPhase
	Verdict      string
	ProposalHash string
}

// Input is everything the policy engine needs to decide.
type Input struct {
	// Name of the action being evaluated (excluded from History). Empty for dry runs.
	Name             string
	Spec             v1.RemediationActionSpec
	Policy           v1.AegisPolicySpec
	PolicyGeneration int64
	Target           TargetState
	History          []ActionRecord
	Simulation       *SimulationState
	// RevertTarget is the action referenced by Spec.RevertOf.
	RevertTarget *ActionRecord
	Now          time.Time
	// ExecutionRecheck re-validates an admitted action at time of use; admission
	// budgets (which already counted this action) are skipped.
	ExecutionRecheck bool
}
