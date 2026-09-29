package approval

import (
	"errors"
	"testing"
	"time"
)

var key = []byte("0123456789abcdef0123456789abcdef-test")

func newV(t *testing.T, now time.Time) *Verifier {
	v, err := NewVerifier(key)
	if err != nil {
		t.Fatal(err)
	}
	v.now = func() time.Time { return now }
	return v
}

func TestDecisionRoundTrip(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	v := newV(t, now)
	d := Decision{ActionName: "act-1", SpecHash: "abc", Decision: "approved", Approver: "alice", Reason: "ok", IssuedAt: now.Unix()}
	d.Signature = v.SignDecision(d)
	if err := v.VerifyDecision(d); err != nil {
		t.Fatalf("valid signature rejected: %v", err)
	}
}

func TestTamperingDetected(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	v := newV(t, now)
	d := Decision{ActionName: "act-1", SpecHash: "abc", Decision: "rejected", Approver: "alice", IssuedAt: now.Unix()}
	d.Signature = v.SignDecision(d)

	for name, mutate := range map[string]func(*Decision){
		"flip decision": func(x *Decision) { x.Decision = "approved" },
		"swap action":   func(x *Decision) { x.ActionName = "act-2" },
		"swap spec":     func(x *Decision) { x.SpecHash = "zzz" },
		"swap approver": func(x *Decision) { x.Approver = "mallory" },
		"edit reason":   func(x *Decision) { x.Reason = "changed" },
	} {
		c := d
		mutate(&c)
		if err := v.VerifyDecision(c); !errors.Is(err, ErrInvalidSignature) {
			t.Errorf("%s: expected invalid signature, got %v", name, err)
		}
	}
}

func TestFreshness(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	v := newV(t, now)
	old := Decision{ActionName: "a", SpecHash: "h", Decision: "approved", Approver: "bob", IssuedAt: now.Add(-10 * time.Minute).Unix()}
	old.Signature = v.SignDecision(old)
	if err := v.VerifyDecision(old); err == nil {
		t.Fatal("expired approval accepted")
	}
	future := old
	future.IssuedAt = now.Add(time.Hour).Unix()
	future.Signature = v.SignDecision(future)
	if err := v.VerifyDecision(future); err == nil {
		t.Fatal("future approval accepted")
	}
}

func TestShortKeyRejected(t *testing.T) {
	if _, err := NewVerifier([]byte("short")); err == nil {
		t.Fatal("short key accepted")
	}
}

func TestModeChange(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	v := newV(t, now)
	m := ModeChange{Mode: "observe", Approver: "admin", IssuedAt: now.Unix()}
	m.Signature = v.SignModeChange(m)
	if err := v.VerifyModeChange(m); err != nil {
		t.Fatal(err)
	}
	m.Mode = "autonomous"
	if err := v.VerifyModeChange(m); err == nil {
		t.Fatal("tampered mode accepted")
	}
}

// Shared test vector: aegis/tests/unit/test_detection_and_security.py signs the
// same inputs with the Python API's signer and expects the same MACs, so the two
// implementations cannot drift apart silently.
var vectorKey = []byte("aegisops-test-vector-key-0123456789abcdef")

func TestCrossLanguageVector(t *testing.T) {
	v, err := NewVerifier(vectorKey)
	if err != nil {
		t.Fatal(err)
	}
	v.now = func() time.Time { return time.Unix(1_800_000_000, 0) }
	d := Decision{ActionName: "act-inc-1-abc123", SpecHash: "0f1e2d3c4b5a69788796a5b4c3d2e1f0", Decision: "approved",
		Approver: "alice", Reason: "rollback verified in sandbox", IssuedAt: 1_800_000_000,
		Signature: "e7d1f17efb87b5c59a35d4c9698090144f929e3d6dd9bb264af166e43113aa25"}
	if err := v.VerifyDecision(d); err != nil {
		t.Fatalf("python-signed decision rejected: %v", err)
	}
	m := ModeChange{Mode: "supervised", Approver: "admin", IssuedAt: 1_800_000_000,
		Signature: "b8b83dbbb99ecddc9e971f63339e36779b7ae560b7f62686ce1cdd4d85b723a9"}
	if err := v.VerifyModeChange(m); err != nil {
		t.Fatalf("python-signed mode change rejected: %v", err)
	}
}
