package httpapi

import (
	"net/http"
	"strconv"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
	"github.com/aegisops/aegisops/controlplane/internal/registry"
)

func (s *Server) nsParam(w http.ResponseWriter, r *http.Request, ns string) (string, bool) {
	if ns == "" && len(s.Namespaces) > 0 {
		ns = s.Namespaces[0]
	}
	if !s.namespaceAllowed(ns) {
		writeError(w, r, http.StatusForbidden, "forbidden", "namespace is outside the observed scope")
		return "", false
	}
	return ns, true
}

func sinceParam(r *http.Request, def time.Duration) (time.Time, error) {
	raw := r.URL.Query().Get("since")
	if raw == "" {
		return time.Now().Add(-def), nil
	}
	return time.Parse(time.RFC3339, raw)
}

func (s *Server) listWorkloads(w http.ResponseWriter, r *http.Request) {
	ns, ok := s.nsParam(w, r, r.URL.Query().Get("namespace"))
	if !ok {
		return
	}
	out, err := s.Resolver.Workloads(r.Context(), ns)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"namespace": ns, "items": out})
}

func (s *Server) getWorkload(w http.ResponseWriter, r *http.Request) {
	ns, ok := s.nsParam(w, r, r.PathValue("namespace"))
	if !ok {
		return
	}
	out, err := s.Resolver.Workload(r.Context(), ns, r.PathValue("name"))
	if apierrors.IsNotFound(err) {
		writeError(w, r, http.StatusNotFound, "not_found", "workload not found")
		return
	}
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) topology(w http.ResponseWriter, r *http.Request) {
	ns, ok := s.nsParam(w, r, r.URL.Query().Get("namespace"))
	if !ok {
		return
	}
	t, err := s.Resolver.Topology(r.Context(), ns)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) changes(w http.ResponseWriter, r *http.Request) {
	since, err := sinceParam(r, time.Hour)
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", "since must be RFC3339")
		return
	}
	ns := r.URL.Query().Get("namespace")
	if ns != "" && !s.namespaceAllowed(ns) {
		writeError(w, r, http.StatusForbidden, "forbidden", "namespace is outside the observed scope")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": s.Changes.Changes(since, ns, r.URL.Query().Get("name"))})
}

func (s *Server) signals(w http.ResponseWriter, r *http.Request) {
	since, err := sinceParam(r, 30*time.Minute)
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", "since must be RFC3339")
		return
	}
	ns := r.URL.Query().Get("namespace")
	if ns != "" && !s.namespaceAllowed(ns) {
		writeError(w, r, http.StatusForbidden, "forbidden", "namespace is outside the observed scope")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": s.Changes.Signals(since, ns, r.URL.Query().Get("name"))})
}

func (s *Server) getPolicy(w http.ResponseWriter, r *http.Request) {
	var pol v1.AegisPolicy
	err := s.Client.Get(r.Context(), client.ObjectKey{Name: s.PolicyName}, &pol)
	if err != nil && !apierrors.IsNotFound(err) {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	spec, gen := pol.Spec, pol.Generation
	source := "AegisPolicy/" + s.PolicyName
	if apierrors.IsNotFound(err) {
		spec, gen, source = v1.DefaultPolicySpec(), 0, "built-in default (fail closed)"
	}
	hist, _, herr := controllersHistory(s, r)
	breaker := map[string]any{"state": "unknown"}
	if herr == nil {
		open, failures, _ := policy.BreakerState(hist, spec.Budgets.CircuitBreaker, time.Now())
		state := "closed"
		if open {
			state = "open"
		}
		breaker = map[string]any{"state": state, "recentFailures": failures}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"source": source, "generation": gen, "spec": spec, "circuitBreaker": breaker, "registry": registry.All(),
	})
}

func (s *Server) auditEvents(w http.ResponseWriter, r *http.Request) {
	after, _ := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
	if b := r.URL.Query().Get("boot"); b != "" && b != s.Audit.Boot() {
		after = 0 // controller restarted: sequence numbers were reset
	}
	writeJSON(w, http.StatusOK, map[string]any{"boot": s.Audit.Boot(), "items": s.Audit.Since(after)})
}
