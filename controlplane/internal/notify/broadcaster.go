// Package notify lets HTTP long-poll handlers wait for object changes observed
// by informers, so API clients get event-driven updates without polling loops.
package notify

import "sync"

// Broadcaster fans out change notifications keyed by object identity.
type Broadcaster struct {
	mu      sync.Mutex
	waiters map[string][]chan struct{}
}

// New returns an empty broadcaster.
func New() *Broadcaster { return &Broadcaster{waiters: map[string][]chan struct{}{}} }

// Wait returns a channel that is closed on the next Notify(key).
// Register before reading current state to avoid missing an update.
func (b *Broadcaster) Wait(key string) <-chan struct{} {
	ch := make(chan struct{})
	b.mu.Lock()
	b.waiters[key] = append(b.waiters[key], ch)
	b.mu.Unlock()
	return ch
}

// Notify wakes every waiter on key.
func (b *Broadcaster) Notify(key string) {
	b.mu.Lock()
	ws := b.waiters[key]
	delete(b.waiters, key)
	b.mu.Unlock()
	for _, ch := range ws {
		close(ch)
	}
}

// Forget drops a waiter that timed out (prevents unbounded growth).
func (b *Broadcaster) Forget(key string, ch <-chan struct{}) {
	b.mu.Lock()
	defer b.mu.Unlock()
	ws := b.waiters[key]
	for i, w := range ws {
		if w == ch {
			b.waiters[key] = append(ws[:i], ws[i+1:]...)
			break
		}
	}
	if len(b.waiters[key]) == 0 {
		delete(b.waiters, key)
	}
}
