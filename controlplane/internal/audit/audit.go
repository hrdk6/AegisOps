// Package audit records security-relevant control-plane decisions. Events are
// emitted as structured log lines and kept in a bounded ring that the AegisOps
// engine ingests into its hash-chained, append-only audit table.
package audit

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"

	"github.com/go-logr/logr"
)

// Event is one audit record.
type Event struct {
	Seq      int64          `json:"seq"`
	Time     time.Time      `json:"time"`
	Actor    string         `json:"actor"`
	Action   string         `json:"action"`
	Resource string         `json:"resource"`
	Outcome  string         `json:"outcome"`
	Details  map[string]any `json:"details,omitempty"`
}

// Log is a bounded in-memory audit ring.
type Log struct {
	mu   sync.Mutex
	boot string
	seq  int64
	ring []Event
	max  int
	log  logr.Logger
}

// New creates a log keeping at most max events.
func New(log logr.Logger, max int) *Log {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return &Log{boot: hex.EncodeToString(b), max: max, log: log.WithName("audit")}
}

// Boot identifies this process lifetime; sequence numbers restart with it.
func (l *Log) Boot() string { return l.boot }

// Record appends an event.
func (l *Log) Record(actor, action, resource, outcome string, details map[string]any) {
	l.mu.Lock()
	l.seq++
	ev := Event{Seq: l.seq, Time: time.Now().UTC(), Actor: actor, Action: action, Resource: resource, Outcome: outcome, Details: details}
	l.ring = append(l.ring, ev)
	if len(l.ring) > l.max {
		l.ring = l.ring[len(l.ring)-l.max:]
	}
	l.mu.Unlock()
	l.log.Info("audit", "seq", ev.Seq, "actor", actor, "action", action, "resource", resource, "outcome", outcome, "details", details)
}

// Since returns events with Seq > after.
func (l *Log) Since(after int64) []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := []Event{}
	for _, e := range l.ring {
		if e.Seq > after {
			out = append(out, e)
		}
	}
	return out
}
