package httpapi

import (
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/controllers"
)

type simulationRequest struct {
	IncidentID string                `json:"incidentId"`
	Target     v1.TargetRef          `json:"target"`
	Proposal   v1.SimulationProposal `json:"proposal"`
	Load       v1.LoadProfile        `json:"load"`
}

// SimulationView is the API representation of a RemediationSimulation.
type SimulationView struct {
	Name            string                         `json:"name"`
	ResourceVersion string                         `json:"resourceVersion"`
	IncidentID      string                         `json:"incidentId"`
	Target          v1.TargetRef                   `json:"target"`
	Proposal        v1.SimulationProposal          `json:"proposal"`
	Load            v1.LoadProfile                 `json:"load"`
	Status          v1.RemediationSimulationStatus `json:"status"`
	CreatedAt       time.Time                      `json:"createdAt"`
}

func simView(s *v1.RemediationSimulation) SimulationView {
	v := SimulationView{Name: s.Name, ResourceVersion: s.ResourceVersion, IncidentID: s.Spec.IncidentID, Target: s.Spec.Target,
		Proposal: s.Spec.Proposal, Load: s.Spec.Load, Status: s.Status, CreatedAt: s.CreationTimestamp.Time}
	if v.Status.Phase == "" {
		v.Status.Phase = v1.SimPending
	}
	return v
}

func (s *Server) submitSimulation(w http.ResponseWriter, r *http.Request) {
	var req simulationRequest
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if !incidentRe.MatchString(req.IncidentID) {
		writeError(w, r, http.StatusBadRequest, "bad_request", "invalid incidentId")
		return
	}
	if !s.namespaceAllowed(req.Target.Namespace) || req.Target.Kind != "Deployment" {
		writeError(w, r, http.StatusBadRequest, "bad_request", "simulation target must be a Deployment in an observed namespace")
		return
	}
	if req.Load.RPS <= 0 {
		req.Load.RPS = 20
	}
	if req.Load.DurationSeconds <= 0 {
		req.Load.DurationSeconds = 15
	}
	if req.Load.Concurrency <= 0 {
		req.Load.Concurrency = 8
	}
	caller := identityFrom(r.Context())
	name := fmt.Sprintf("sim-%s-%s", strings.ToLower(req.IncidentID), randomSuffix())
	if len(name) > 63 {
		name = "sim-" + randomSuffix() + randomSuffix()
	}
	sim := &v1.RemediationSimulation{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.SystemNamespace, Name: name,
			Labels:      map[string]string{"aegisops.io/incident": strings.ToLower(req.IncidentID)},
			Annotations: map[string]string{controllers.AnnotationTraceParent: traceParent(r.Context())}},
		Spec: v1.RemediationSimulationSpec{IncidentID: req.IncidentID, Target: req.Target, Proposal: req.Proposal,
			Load: req.Load, RequestedBy: caller.Subject},
	}
	if err := s.Client.Create(r.Context(), sim); err != nil {
		if apierrors.IsInvalid(err) {
			writeError(w, r, http.StatusBadRequest, "invalid_simulation", err.Error())
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.Audit.Record(caller.Subject, "simulation.submit", "remediationsimulation/"+name, "created",
		map[string]any{"target": req.Target.Key(), "proposal": req.Proposal.ActionType})
	writeJSON(w, http.StatusCreated, simView(sim))
}

func (s *Server) getSimulation(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	key := client.ObjectKey{Namespace: s.SystemNamespace, Name: name}
	waitFor := r.URL.Query().Get("waitForVersion")
	timeout, _ := strconv.Atoi(r.URL.Query().Get("timeoutSeconds"))
	if timeout <= 0 || timeout > 30 {
		timeout = 25
	}
	var ch <-chan struct{}
	if waitFor != "" {
		ch = s.Notify.Wait("simulation/" + name)
		defer s.Notify.Forget("simulation/"+name, ch)
	}
	var sim v1.RemediationSimulation
	if err := s.Client.Get(r.Context(), key, &sim); err != nil {
		if apierrors.IsNotFound(err) {
			writeError(w, r, http.StatusNotFound, "not_found", "simulation not found")
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if waitFor != "" && sim.ResourceVersion == waitFor {
		select {
		case <-ch:
			_ = s.Client.Get(r.Context(), key, &sim)
		case <-time.After(time.Duration(timeout) * time.Second):
		case <-r.Context().Done():
			return
		}
	}
	writeJSON(w, http.StatusOK, simView(&sim))
}

func (s *Server) listSimulations(w http.ResponseWriter, r *http.Request) {
	var list v1.RemediationSimulationList
	opts := []client.ListOption{client.InNamespace(s.SystemNamespace)}
	if inc := r.URL.Query().Get("incident"); inc != "" {
		opts = append(opts, client.MatchingLabels{"aegisops.io/incident": strings.ToLower(inc)})
	}
	if err := s.Client.List(r.Context(), &list, opts...); err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]SimulationView, 0, len(list.Items))
	for i := range list.Items {
		out = append(out, simView(&list.Items[i]))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	writeJSON(w, http.StatusOK, map[string]any{"items": out})
}

func (s *Server) listCanaries(w http.ResponseWriter, r *http.Request) {
	ns, ok := s.nsParam(w, r, r.URL.Query().Get("namespace"))
	if !ok {
		return
	}
	var list v1.CanaryReleaseList
	if err := s.Client.List(r.Context(), &list, client.InNamespace(ns)); err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	type canaryView struct {
		Name      string                 `json:"name"`
		Namespace string                 `json:"namespace"`
		Spec      v1.CanaryReleaseSpec   `json:"spec"`
		Status    v1.CanaryReleaseStatus `json:"status"`
		CreatedAt time.Time              `json:"createdAt"`
	}
	out := make([]canaryView, 0, len(list.Items))
	for _, c := range list.Items {
		out = append(out, canaryView{Name: c.Name, Namespace: c.Namespace, Spec: c.Spec, Status: c.Status, CreatedAt: c.CreationTimestamp.Time})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	writeJSON(w, http.StatusOK, map[string]any{"items": out})
}
