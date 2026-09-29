// Package approval verifies human approval decisions.
//
// Approvals are HMAC-SHA256 signed by the AegisOps API (which authenticates the
// human user) with a key that is mounted only into the API and the controller.
// The AI engine never holds the key, so even a fully compromised engine cannot
// forge an approval. Signatures bind the decision to the action's spec hash so
// an approved action cannot be swapped for a different one.
package approval

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// MaxClockSkew bounds how far in the future an approval may be issued.
const MaxClockSkew = 30 * time.Second

// MaxAge bounds how old an approval may be when presented.
const MaxAge = 5 * time.Minute

// ErrInvalidSignature is returned for any verification failure.
var ErrInvalidSignature = errors.New("invalid approval signature")

// Decision is a signed human decision on one action.
type Decision struct {
	ActionName string `json:"actionName"`
	SpecHash   string `json:"specHash"`
	Decision   string `json:"decision"` // approved | rejected
	Approver   string `json:"approver"`
	Reason     string `json:"reason"`
	IssuedAt   int64  `json:"issuedAt"` // unix seconds
	Signature  string `json:"signature"`
}

// ModeChange is a signed request to change the automation mode.
type ModeChange struct {
	Mode      string `json:"mode"`
	Approver  string `json:"approver"`
	IssuedAt  int64  `json:"issuedAt"`
	Signature string `json:"signature"`
}

// Verifier checks signatures with a shared secret.
type Verifier struct {
	key []byte
	now func() time.Time
}

// NewVerifier returns a verifier; the key must be at least 32 bytes.
func NewVerifier(key []byte) (*Verifier, error) {
	if len(key) < 32 {
		return nil, fmt.Errorf("approval signing key must be at least 32 bytes (got %d)", len(key))
	}
	return &Verifier{key: key, now: time.Now}, nil
}

func (d Decision) payload() string {
	reasonHash := sha256.Sum256([]byte(d.Reason))
	return strings.Join([]string{
		"aegisops-approval-v1", d.ActionName, d.SpecHash, d.Decision, d.Approver,
		strconv.FormatInt(d.IssuedAt, 10), hex.EncodeToString(reasonHash[:]),
	}, "\n")
}

func (m ModeChange) payload() string {
	return strings.Join([]string{"aegisops-mode-v1", m.Mode, m.Approver, strconv.FormatInt(m.IssuedAt, 10)}, "\n")
}

func (v *Verifier) mac(payload string) string {
	h := hmac.New(sha256.New, v.key)
	h.Write([]byte(payload))
	return hex.EncodeToString(h.Sum(nil))
}

func (v *Verifier) checkTime(issued int64) error {
	t := time.Unix(issued, 0)
	now := v.now()
	if t.After(now.Add(MaxClockSkew)) {
		return fmt.Errorf("%w: issued in the future", ErrInvalidSignature)
	}
	if now.Sub(t) > MaxAge {
		return fmt.Errorf("%w: expired", ErrInvalidSignature)
	}
	return nil
}

// SignDecision computes the signature (used by tests and dev tooling).
func (v *Verifier) SignDecision(d Decision) string { return v.mac(d.payload()) }

// SignModeChange computes the signature for a mode change.
func (v *Verifier) SignModeChange(m ModeChange) string { return v.mac(m.payload()) }

// VerifyDecision validates the signature, freshness and decision value.
func (v *Verifier) VerifyDecision(d Decision) error {
	if d.Decision != "approved" && d.Decision != "rejected" {
		return fmt.Errorf("%w: decision must be approved or rejected", ErrInvalidSignature)
	}
	if d.Approver == "" {
		return fmt.Errorf("%w: approver required", ErrInvalidSignature)
	}
	if err := v.checkTime(d.IssuedAt); err != nil {
		return err
	}
	if !hmac.Equal([]byte(v.mac(d.payload())), []byte(strings.ToLower(d.Signature))) {
		return ErrInvalidSignature
	}
	return nil
}

// VerifyModeChange validates a mode-change request.
func (v *Verifier) VerifyModeChange(m ModeChange) error {
	if m.Approver == "" {
		return fmt.Errorf("%w: approver required", ErrInvalidSignature)
	}
	if err := v.checkTime(m.IssuedAt); err != nil {
		return err
	}
	if !hmac.Equal([]byte(v.mac(m.payload())), []byte(strings.ToLower(m.Signature))) {
		return ErrInvalidSignature
	}
	return nil
}
