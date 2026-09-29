// Package httpapi is the authenticated control-plane API. It is the only way
// the AI layer interacts with Kubernetes: read-only state views, dry-run
// policy evaluation, and *requests* for actions/simulations that the
// controller alone admits and executes.
package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/go-logr/logr"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"golang.org/x/time/rate"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/aegisops/aegisops/controlplane/internal/approval"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/changelog"
	"github.com/aegisops/aegisops/controlplane/internal/controllers"
	"github.com/aegisops/aegisops/controlplane/internal/notify"
	"github.com/aegisops/aegisops/controlplane/internal/state"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

const maxBodyBytes = 64 << 10

// Server holds API dependencies.
type Server struct {
	Addr            string
	Client          client.Client
	Auth            Authenticator
	Verifier        *approval.Verifier
	Evaluator       *controllers.Evaluator
	Resolver        *state.Resolver
	Changes         *changelog.Recorder
	Audit           *audit.Log
	Notify          *notify.Broadcaster
	PolicyName      string
	SystemNamespace string
	Namespaces      []string
	Log             logr.Logger
	// DecisionWait bounds how long submit waits for the admission decision.
	DecisionWait time.Duration

	limMu    sync.Mutex
	limiters map[string]*rate.Limiter
}

// NeedLeaderElection lets every replica serve the API.
func (s *Server) NeedLeaderElection() bool { return false }

type ctxKey int

const (
	ctxIdentity ctxKey = iota
	ctxRequestID
)

func identityFrom(ctx context.Context) *Identity {
	id, _ := ctx.Value(ctxIdentity).(*Identity)
	return id
}

func requestID(ctx context.Context) string {
	id, _ := ctx.Value(ctxRequestID).(string)
	return id
}

// Handler builds the routed, instrumented handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	route := func(pattern string, role Role, h http.HandlerFunc) {
		mux.Handle(pattern, s.withAuth(role, pattern, h))
	}
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "boot": s.Audit.Boot()})
	})
	route("GET /v1/workloads", RoleReader, s.listWorkloads)
	route("GET /v1/workloads/{namespace}/{name}", RoleReader, s.getWorkload)
	route("GET /v1/topology", RoleReader, s.topology)
	route("GET /v1/changes", RoleReader, s.changes)
	route("GET /v1/signals", RoleReader, s.signals)
	route("GET /v1/policy", RoleReader, s.getPolicy)
	route("POST /v1/policy/evaluate", RoleReader, s.evaluate)
	route("PUT /v1/policy/mode", RoleApprover, s.setMode)
	route("GET /v1/audit", RoleReader, s.auditEvents)
	route("GET /v1/actions", RoleReader, s.listActions)
	route("POST /v1/actions", RoleProposer, s.submitAction)
	route("GET /v1/actions/{name}", RoleReader, s.getAction)
	route("POST /v1/actions/{name}/approval", RoleApprover, s.approve)
	route("POST /v1/actions/{name}/revert", RoleProposer, s.revert)
	route("GET /v1/simulations", RoleReader, s.listSimulations)
	route("POST /v1/simulations", RoleProposer, s.submitSimulation)
	route("GET /v1/simulations/{name}", RoleReader, s.getSimulation)
	route("GET /v1/canaries", RoleReader, s.listCanaries)
	return otelhttp.NewHandler(s.withRequestID(mux), "controlplane-api",
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string { return r.Method + " " + r.URL.Path }))
}

func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if id == "" || len(id) > 64 {
			b := make([]byte, 8)
			_, _ = rand.Read(b)
			id = hex.EncodeToString(b)
		}
		w.Header().Set("X-Request-ID", id)
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, code: 200}
		next.ServeHTTP(sw, r.WithContext(context.WithValue(r.Context(), ctxRequestID, id)))
		s.Log.V(1).Info("request", "method", r.Method, "path", r.URL.Path, "status", sw.code,
			"durationMs", time.Since(start).Milliseconds(), "requestId", id)
	})
}

type statusWriter struct {
	http.ResponseWriter
	code int
}

func (w *statusWriter) WriteHeader(code int) {
	w.code = code
	w.ResponseWriter.WriteHeader(code)
}

func (s *Server) limiter(subject string) *rate.Limiter {
	s.limMu.Lock()
	defer s.limMu.Unlock()
	if s.limiters == nil {
		s.limiters = map[string]*rate.Limiter{}
	}
	l, ok := s.limiters[subject]
	if !ok {
		l = rate.NewLimiter(rate.Limit(30), 60)
		s.limiters[subject] = l
	}
	return l
}

func (s *Server) withAuth(role Role, pattern string, h http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		code := http.StatusOK
		defer func() { telemetry.APIRequests.WithLabelValues(pattern, fmt.Sprint(code)).Inc() }()
		tok, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !ok || tok == "" {
			code = http.StatusUnauthorized
			writeError(w, r, code, "unauthenticated", "missing bearer token")
			return
		}
		id, err := s.Auth.Authenticate(r.Context(), tok)
		if err != nil {
			code = http.StatusUnauthorized
			s.Audit.Record("unknown", "api.authenticate", pattern, "denied", map[string]any{"error": err.Error()})
			writeError(w, r, code, "unauthenticated", "invalid credentials")
			return
		}
		if !id.Has(role) {
			code = http.StatusForbidden
			s.Audit.Record(id.Subject, "api.authorize", pattern, "denied", map[string]any{"requiredRole": role})
			writeError(w, r, code, "forbidden", fmt.Sprintf("role %q required", role))
			return
		}
		if !s.limiter(id.Subject).Allow() {
			code = http.StatusTooManyRequests
			writeError(w, r, code, "rate_limited", "too many requests")
			return
		}
		sw := &statusWriter{ResponseWriter: w, code: 200}
		h(sw, r.WithContext(context.WithValue(r.Context(), ctxIdentity, id)))
		code = sw.code
	})
}

func (s *Server) namespaceAllowed(ns string) bool {
	for _, n := range s.Namespaces {
		if n == ns {
			return true
		}
	}
	return false
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, r *http.Request, code int, kind, msg string) {
	writeJSON(w, code, map[string]any{"error": map[string]string{"code": kind, "message": msg}, "requestId": requestID(r.Context())})
}

// decode strictly decodes JSON (unknown fields are rejected).
func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			return fmt.Errorf("request body too large")
		}
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if dec.More() {
		return fmt.Errorf("unexpected trailing data")
	}
	_, _ = io.Copy(io.Discard, r.Body)
	return nil
}

// Start implements manager.Runnable.
func (s *Server) Start(ctx context.Context) error {
	srv := &http.Server{Addr: s.Addr, Handler: s.Handler(), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 15 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 90 * time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()
	s.Log.Info("control-plane API listening", "addr", s.Addr)
	select {
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdown)
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}
