package httpapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"time"

	authnv1 "k8s.io/api/authentication/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Role is a control-plane API permission.
type Role string

// Roles. The AI engine holds reader+proposer; only the AegisOps API (which
// authenticates humans) holds approver.
const (
	RoleReader   Role = "reader"
	RoleProposer Role = "proposer"
	RoleApprover Role = "approver"
)

// Identity is an authenticated caller.
type Identity struct {
	Subject string
	Roles   map[Role]bool
}

// Has reports whether the identity holds role.
func (i *Identity) Has(r Role) bool { return i != nil && i.Roles[r] }

// Authenticator validates bearer tokens.
type Authenticator interface {
	Authenticate(ctx context.Context, token string) (*Identity, error)
}

// ParseBindings parses "subject=role,role;subject2=role".
func ParseBindings(s string) (map[string][]Role, error) {
	out := map[string][]Role{}
	for _, entry := range strings.Split(s, ";") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		subj, roles, ok := strings.Cut(entry, "=")
		if !ok {
			return nil, fmt.Errorf("invalid binding %q", entry)
		}
		for _, r := range strings.Split(roles, ",") {
			role := Role(strings.TrimSpace(r))
			switch role {
			case RoleReader, RoleProposer, RoleApprover:
				out[strings.TrimSpace(subj)] = append(out[strings.TrimSpace(subj)], role)
			default:
				return nil, fmt.Errorf("unknown role %q", r)
			}
		}
	}
	return out, nil
}

func identity(subject string, roles []Role) *Identity {
	id := &Identity{Subject: subject, Roles: map[Role]bool{}}
	for _, r := range roles {
		id.Roles[r] = true
	}
	return id
}

// TokenReviewAuthenticator validates projected ServiceAccount tokens with the
// Kubernetes TokenReview API (audience-bound) and maps the resulting username
// to roles. Results are cached briefly, keyed by the token's hash.
type TokenReviewAuthenticator struct {
	Client   client.Client
	Audience string
	Bindings map[string][]Role
	TTL      time.Duration

	mu    sync.Mutex
	cache map[string]cacheEntry
}

type cacheEntry struct {
	id  *Identity
	exp time.Time
}

// Authenticate implements Authenticator.
func (a *TokenReviewAuthenticator) Authenticate(ctx context.Context, token string) (*Identity, error) {
	sum := sha256.Sum256([]byte(token))
	key := hex.EncodeToString(sum[:])
	a.mu.Lock()
	if a.cache == nil {
		a.cache = map[string]cacheEntry{}
	}
	if e, ok := a.cache[key]; ok && time.Now().Before(e.exp) {
		a.mu.Unlock()
		return e.id, nil
	}
	a.mu.Unlock()

	tr := &authnv1.TokenReview{Spec: authnv1.TokenReviewSpec{Token: token, Audiences: []string{a.Audience}}}
	if err := a.Client.Create(ctx, tr); err != nil {
		return nil, fmt.Errorf("token review failed: %w", err)
	}
	if !tr.Status.Authenticated {
		return nil, fmt.Errorf("token not authenticated")
	}
	roles, ok := a.Bindings[tr.Status.User.Username]
	if !ok {
		return nil, fmt.Errorf("subject %q has no control-plane role", tr.Status.User.Username)
	}
	id := identity(tr.Status.User.Username, roles)
	a.mu.Lock()
	if len(a.cache) > 1024 {
		a.cache = map[string]cacheEntry{}
	}
	a.cache[key] = cacheEntry{id: id, exp: time.Now().Add(a.TTL)}
	a.mu.Unlock()
	return id, nil
}

// StaticTokenAuthenticator is for local development only (engine running
// outside the cluster). Format: "token=subject:role,role;token2=subject:role".
type StaticTokenAuthenticator struct {
	tokens map[string]*Identity
}

// NewStaticTokenAuthenticator parses the static token specification.
func NewStaticTokenAuthenticator(spec string) (*StaticTokenAuthenticator, error) {
	s := &StaticTokenAuthenticator{tokens: map[string]*Identity{}}
	for _, entry := range strings.Split(spec, ";") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		tok, rest, ok := strings.Cut(entry, "=")
		subj, roles, ok2 := strings.Cut(rest, ":")
		if !ok || !ok2 || len(tok) < 24 {
			return nil, fmt.Errorf("invalid static token entry (tokens must be >= 24 chars)")
		}
		b, err := ParseBindings(subj + "=" + roles)
		if err != nil {
			return nil, err
		}
		s.tokens[tok] = identity(subj, b[subj])
	}
	return s, nil
}

// Authenticate implements Authenticator.
func (s *StaticTokenAuthenticator) Authenticate(_ context.Context, token string) (*Identity, error) {
	for t, id := range s.tokens {
		if subtleEqual(t, token) {
			return id, nil
		}
	}
	return nil, fmt.Errorf("unknown token")
}

// ChainAuthenticator tries each authenticator in order.
type ChainAuthenticator []Authenticator

// Authenticate implements Authenticator.
func (c ChainAuthenticator) Authenticate(ctx context.Context, token string) (*Identity, error) {
	var last error
	for _, a := range c {
		id, err := a.Authenticate(ctx, token)
		if err == nil {
			return id, nil
		}
		last = err
	}
	if last == nil {
		last = fmt.Errorf("no authenticator configured")
	}
	return nil, last
}

func subtleEqual(a, b string) bool {
	ha := sha256.Sum256([]byte(a))
	hb := sha256.Sum256([]byte(b))
	var v byte
	for i := range ha {
		v |= ha[i] ^ hb[i]
	}
	return v == 0
}
