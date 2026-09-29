// Package hashing provides stable content hashes that bind approvals and
// simulations to an exact action specification.
package hashing

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
)

func sum(v any) string {
	// encoding/json sorts map keys and struct fields are emitted in declaration
	// order, so the encoding is deterministic for our types.
	b, err := json.Marshal(v)
	if err != nil {
		panic(err) // only plain data types are hashed
	}
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])[:32]
}

// SpecHash hashes every field of an action spec that affects execution.
func SpecHash(s v1.RemediationActionSpec) string {
	return sum(struct {
		Incident string
		Type     v1.ActionType
		Target   v1.TargetRef
		Params   v1.ActionParameters
		Revert   string
		Sim      string
		Pre      *v1.ActionPreconditions `json:",omitempty"`
	}{s.IncidentID, s.ActionType, s.Target, s.Parameters, s.RevertOf, s.SimulationRef, s.Preconditions})
}

// ProposalHash identifies (target, action, params) independent of incident.
func ProposalHash(target v1.TargetRef, t v1.ActionType, p v1.ActionParameters) string {
	return sum(struct {
		Target v1.TargetRef
		Type   v1.ActionType
		Params v1.ActionParameters
	}{target, t, p})
}
