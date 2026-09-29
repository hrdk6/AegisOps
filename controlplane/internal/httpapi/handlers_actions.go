package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"

	"go.opentelemetry.io/otel/propagation"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/approval"
	"github.com/aegisops/aegisops/controlplane/internal/controllers"
	"github.com/aegisops/aegisops/controlplane/internal/policy"
	"github.com/aegisops/aegisops/controlplane/internal/registry"
)

var incidentRe = regexp.MustCompile(`^[A-Za-z0-9-]{1,64}$`)

type actionRequest struct {
	IncidentID          string                  `json:"incidentId"`
	ActionType          v1.ActionType           `json:"actionType"`
	Target              v1.TargetRef            `json:"target"`
	Parameters          v1.ActionParameters     `json:"parameters"`
	Justification       string                  `json:"justification"`
	DiagnosisConfidence int32                   `json:"diagnosisConfidence"`
	SimulationRef       string                  `json:"simulationRef"`
	Preconditions       *v1.ActionPreconditions `json:"preconditions,omitempty"`
}

// ActionView is the API representation of a RemediationAction.
type ActionView struct {
	Name                string                  `json:"name"`
	UID                 string                  `json:"uid"`
	ResourceVersion     string                  `json:"resourceVersion"`
	IncidentID          string                  `json:"incidentId"`
	ActionType          v1.ActionType           `json:"actionType"`
	Target              v1.TargetRef            `json:"target"`
	Parameters          v1.ActionParameters     `json:"parameters"`
	Justification       string                  `json:"justification,omitempty"`
	DiagnosisConfidence int32                   `json:"diagnosisConfidence"`
	SimulationRef       string                  `json:"simulationRef,omitempty"`
	RevertOf            string                  `json:"revertOf,omitempty"`
	Preconditions       *v1.ActionPreconditions `json:"preconditions,omitempty"`
	RequestedBy         string                  `json:"requestedBy"`
	Phase               v1.ActionPhase          `json:"phase"`
	Decision            *v1.PolicyDecision      `json:"decision,omitempty"`
	Approval            *v1.ApprovalRecord      `json:"approval,omitempty"`
	SnapshotKind        string                  `json:"snapshotKind,omitempty"`
	Reversible          bool                    `json:"reversible"`
	SpecHash            string                  `json:"specHash,omitempty"`
	Message             string                  `json:"message,omitempty"`
	RevertedBy          string                  `json:"revertedBy,omitempty"`
	CreatedAt           time.Time               `json:"createdAt"`
	StartedAt           *time.Time              `json:"startedAt,omitempty"`
	CompletedAt         *time.Time              `json:"completedAt,omitempty"`
}

func view(a *v1.RemediationAction) ActionView {
	v := ActionView{
		Name: a.Name, UID: string(a.UID), ResourceVersion: a.ResourceVersion, IncidentID: a.Spec.IncidentID,
		ActionType: a.Spec.ActionType, Target: a.Spec.Target, Parameters: a.Spec.Parameters, Justification: a.Spec.Justification,
		DiagnosisConfidence: a.Spec.DiagnosisConfidence, SimulationRef: a.Spec.SimulationRef, RevertOf: a.Spec.RevertOf,
		Preconditions: a.Spec.Preconditions, RequestedBy: a.Spec.RequestedBy, Phase: a.Status.Phase, Decision: a.Status.Decision, Approval: a.Status.Approval,
		SpecHash: a.Status.SpecHash, Message: a.Status.Message, RevertedBy: a.Status.RevertedBy, CreatedAt: a.CreationTimestamp.Time,
	}
	if v.Phase == "" {
		v.Phase = v1.PhasePending
	}
	if a.Status.Snapshot != nil {
		v.SnapshotKind, v.Reversible = a.Status.Snapshot.Kind, a.Status.Snapshot.Reversible
	}
	if a.Status.StartedAt != nil {
		t := a.Status.StartedAt.Time
		v.StartedAt = &t
	}
	if a.Status.CompletedAt != nil {
		t := a.Status.CompletedAt.Time
		v.CompletedAt = &t
	}
	return v
}

func sanitizeText(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) && r != '\n' {
			return -1
		}
		return r
	}, s)
	if len(s) > max {
		s = s[:max]
	}
	return s
}

func randomSuffix() string {
	b := make([]byte, 3)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func actionName(incident string) string {
	n := "act-" + strings.ToLower(incident)
	if len(n) > 50 {
		n = n[:50]
	}
	return strings.TrimRight(n, "-") + "-" + randomSuffix()
}

func (req *actionRequest) validate() error {
	if !incidentRe.MatchString(req.IncidentID) {
		return fmt.Errorf("incidentId must match %s", incidentRe)
	}
	if _, ok := registry.Lookup(req.ActionType); !ok {
		return fmt.Errorf("unknown action type %q", req.ActionType)
	}
	if req.DiagnosisConfidence < 0 || req.DiagnosisConfidence > 100 {
		return fmt.Errorf("diagnosisConfidence must be 0-100")
	}
	if req.ActionType == v1.ActionRevert {
		return fmt.Errorf("use POST /v1/actions/{name}/revert to revert an action")
	}
	return nil
}

func traceParent(ctx context.Context) string {
	c := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, c)
	return c["traceparent"]
}

func (s *Server) newAction(ctx context.Context, name string, spec v1.RemediationActionSpec) *v1.RemediationAction {
	return &v1.RemediationAction{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.SystemNamespace, Name: name,
			Labels:      map[string]string{"aegisops.io/incident": strings.ToLower(spec.IncidentID), "aegisops.io/action-type": string(spec.ActionType)},
			Annotations: map[string]string{controllers.AnnotationTraceParent: traceParent(ctx)}},
		Spec: spec,
	}
}

// waitDecision blocks briefly until the reconciler has admitted the action.
func (s *Server) waitDecision(ctx context.Context, name string, timeout time.Duration) (*v1.RemediationAction, error) {
	deadline := time.Now().Add(timeout)
	for {
		ch := s.Notify.Wait("action/" + name)
		var a v1.RemediationAction
		err := s.Client.Get(ctx, client.ObjectKey{Namespace: s.SystemNamespace, Name: name}, &a)
		if err == nil && a.Status.Phase != "" {
			s.Notify.Forget("action/"+name, ch)
			return &a, nil
		}
		if err != nil && !apierrors.IsNotFound(err) {
			s.Notify.Forget("action/"+name, ch)
			return nil, err
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			s.Notify.Forget("action/"+name, ch)
			if err != nil {
				return nil, err
			}
			return &a, nil
		}
		select {
		case <-ch:
		case <-time.After(remaining):
		case <-ctx.Done():
			s.Notify.Forget("action/"+name, ch)
			return nil, ctx.Err()
		}
	}
}

func (s *Server) submitAction(w http.ResponseWriter, r *http.Request) {
	var req actionRequest
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	id := identityFrom(r.Context())
	if err := req.validate(); err != nil {
		s.Audit.Record(id.Subject, "action.submit", "remediationaction", "rejected", map[string]any{"error": err.Error(), "actionType": req.ActionType})
		writeError(w, r, http.StatusBadRequest, "invalid_action", err.Error())
		return
	}
	spec := v1.RemediationActionSpec{
		IncidentID: req.IncidentID, ActionType: req.ActionType, Target: req.Target, Parameters: req.Parameters,
		Justification: sanitizeText(req.Justification, 2000), DiagnosisConfidence: req.DiagnosisConfidence,
		SimulationRef: req.SimulationRef, Preconditions: req.Preconditions, RequestedBy: id.Subject,
	}
	a := s.newAction(r.Context(), actionName(req.IncidentID), spec)
	if err := s.Client.Create(r.Context(), a); err != nil {
		if apierrors.IsInvalid(err) {
			writeError(w, r, http.StatusBadRequest, "invalid_action", err.Error())
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.Audit.Record(id.Subject, "action.submit", "remediationaction/"+a.Name, "created",
		map[string]any{"incident": req.IncidentID, "actionType": req.ActionType, "target": req.Target.Key()})
	admitted, err := s.waitDecision(r.Context(), a.Name, s.decisionWait())
	if err != nil {
		writeJSON(w, http.StatusAccepted, view(a))
		return
	}
	writeJSON(w, http.StatusCreated, view(admitted))
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	var req actionRequest
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if !incidentRe.MatchString(req.IncidentID) {
		req.IncidentID = "DRY-RUN"
	}
	spec := v1.RemediationActionSpec{IncidentID: req.IncidentID, ActionType: req.ActionType, Target: req.Target,
		Parameters: req.Parameters, DiagnosisConfidence: req.DiagnosisConfidence, SimulationRef: req.SimulationRef,
		Preconditions: req.Preconditions, RequestedBy: identityFrom(r.Context()).Subject}
	a := &v1.RemediationAction{Spec: spec}
	in, _, err := s.Evaluator.Input(r.Context(), a, false, time.Now())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	d := policy.Evaluate(in)
	reg, _ := registry.Lookup(req.ActionType)
	writeJSON(w, http.StatusOK, map[string]any{"decision": d, "action": reg, "target": in.Target})
}

func (s *Server) getAction(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	key := client.ObjectKey{Namespace: s.SystemNamespace, Name: name}
	waitFor := r.URL.Query().Get("waitForVersion")
	timeout, _ := strconv.Atoi(r.URL.Query().Get("timeoutSeconds"))
	if timeout <= 0 || timeout > 30 {
		timeout = 25
	}
	var ch <-chan struct{}
	if waitFor != "" {
		ch = s.Notify.Wait("action/" + name)
		defer s.Notify.Forget("action/"+name, ch)
	}
	var a v1.RemediationAction
	if err := s.Client.Get(r.Context(), key, &a); err != nil {
		if apierrors.IsNotFound(err) {
			writeError(w, r, http.StatusNotFound, "not_found", "action not found")
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if waitFor != "" && a.ResourceVersion == waitFor {
		select {
		case <-ch:
			_ = s.Client.Get(r.Context(), key, &a)
		case <-time.After(time.Duration(timeout) * time.Second):
		case <-r.Context().Done():
			return
		}
	}
	writeJSON(w, http.StatusOK, view(&a))
}

func (s *Server) listActions(w http.ResponseWriter, r *http.Request) {
	var list v1.RemediationActionList
	opts := []client.ListOption{client.InNamespace(s.SystemNamespace)}
	if inc := r.URL.Query().Get("incident"); inc != "" {
		opts = append(opts, client.MatchingLabels{"aegisops.io/incident": strings.ToLower(inc)})
	}
	if err := s.Client.List(r.Context(), &list, opts...); err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]ActionView, 0, len(list.Items))
	for i := range list.Items {
		out = append(out, view(&list.Items[i]))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	writeJSON(w, http.StatusOK, map[string]any{"items": out})
}

type httpErr struct {
	code       int
	kind, text string
}

type approvalRequest struct {
	SpecHash  string `json:"specHash"`
	Decision  string `json:"decision"`
	Approver  string `json:"approver"`
	Reason    string `json:"reason"`
	IssuedAt  int64  `json:"issuedAt"`
	Signature string `json:"signature"`
}

func (s *Server) approve(w http.ResponseWriter, r *http.Request) {
	var req approvalRequest
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	name := r.PathValue("name")
	caller := identityFrom(r.Context())
	d := approval.Decision{ActionName: name, SpecHash: req.SpecHash, Decision: req.Decision, Approver: req.Approver,
		Reason: req.Reason, IssuedAt: req.IssuedAt, Signature: req.Signature}
	if err := s.Verifier.VerifyDecision(d); err != nil {
		s.Audit.Record(caller.Subject, "approval.verify", "remediationaction/"+name, "rejected_signature",
			map[string]any{"approver": req.Approver, "error": err.Error()})
		writeError(w, r, http.StatusForbidden, "invalid_signature", err.Error())
		return
	}
	var out *v1.RemediationAction
	var apiErr *httpErr
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var a v1.RemediationAction
		if err := s.Client.Get(r.Context(), client.ObjectKey{Namespace: s.SystemNamespace, Name: name}, &a); err != nil {
			return err
		}
		if a.Status.Phase != v1.PhaseAwaitingApproval {
			apiErr = &httpErr{http.StatusConflict, "invalid_state", fmt.Sprintf("action is %s, not AwaitingApproval", a.Status.Phase)}
			return nil
		}
		if a.Status.SpecHash != req.SpecHash {
			apiErr = &httpErr{http.StatusConflict, "spec_mismatch", "approval was signed for a different action spec"}
			return nil
		}
		now := metav1.Now()
		a.Status.Approval = &v1.ApprovalRecord{Decision: req.Decision, Approver: req.Approver, Reason: sanitizeText(req.Reason, 500),
			DecidedAt: now, SignatureVerified: true}
		if req.Decision == "approved" {
			a.Status.Phase = v1.PhaseApproved
			a.Status.Message = "approved by " + req.Approver
		} else {
			a.Status.Phase = v1.PhaseRejected
			a.Status.Message = "rejected by " + req.Approver
			a.Status.CompletedAt = &now
		}
		if err := s.Client.Status().Update(r.Context(), &a); err != nil {
			return err
		}
		out = &a
		return nil
	})
	if apiErr != nil {
		writeError(w, r, apiErr.code, apiErr.kind, apiErr.text)
		return
	}
	if err != nil {
		if apierrors.IsNotFound(err) {
			writeError(w, r, http.StatusNotFound, "not_found", "action not found")
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.Audit.Record(req.Approver, "approval."+req.Decision, "remediationaction/"+name, req.Decision,
		map[string]any{"via": caller.Subject, "reason": req.Reason})
	writeJSON(w, http.StatusOK, view(out))
}

type revertRequest struct {
	Justification string `json:"justification"`
}

func (s *Server) revert(w http.ResponseWriter, r *http.Request) {
	var req revertRequest
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	var orig v1.RemediationAction
	if err := s.Client.Get(r.Context(), client.ObjectKey{Namespace: s.SystemNamespace, Name: r.PathValue("name")}, &orig); err != nil {
		if apierrors.IsNotFound(err) {
			writeError(w, r, http.StatusNotFound, "not_found", "action not found")
			return
		}
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	caller := identityFrom(r.Context())
	name := orig.Name + "-revert"
	if len(name) > 253 {
		name = name[:253]
	}
	spec := v1.RemediationActionSpec{IncidentID: orig.Spec.IncidentID, ActionType: v1.ActionRevert, Target: orig.Spec.Target,
		RevertOf: orig.Name, RequestedBy: caller.Subject, Justification: sanitizeText(req.Justification, 2000)}
	a := s.newAction(r.Context(), name, spec)
	if err := s.Client.Create(r.Context(), a); err != nil && !apierrors.IsAlreadyExists(err) {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.Audit.Record(caller.Subject, "action.revert.request", "remediationaction/"+orig.Name, "requested", map[string]any{"revert": name})
	admitted, err := s.waitDecision(r.Context(), name, s.decisionWait())
	if err != nil {
		writeJSON(w, http.StatusAccepted, view(a))
		return
	}
	writeJSON(w, http.StatusCreated, view(admitted))
}

func (s *Server) setMode(w http.ResponseWriter, r *http.Request) {
	var req approval.ModeChange
	if err := decode(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	caller := identityFrom(r.Context())
	if req.Mode != v1.ModeAutonomous && req.Mode != v1.ModeSupervised && req.Mode != v1.ModeObserve {
		writeError(w, r, http.StatusBadRequest, "bad_request", "mode must be autonomous, supervised or observe")
		return
	}
	if err := s.Verifier.VerifyModeChange(req); err != nil {
		s.Audit.Record(caller.Subject, "policy.mode", "aegispolicy/"+s.PolicyName, "rejected_signature", map[string]any{"error": err.Error()})
		writeError(w, r, http.StatusForbidden, "invalid_signature", err.Error())
		return
	}
	var prev string
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var pol v1.AegisPolicy
		if err := s.Client.Get(r.Context(), client.ObjectKey{Name: s.PolicyName}, &pol); err != nil {
			return err
		}
		prev = pol.Spec.Mode
		pol.Spec.Mode = req.Mode
		return s.Client.Update(r.Context(), &pol)
	})
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.Audit.Record(req.Approver, "policy.mode", "aegispolicy/"+s.PolicyName, "changed",
		map[string]any{"from": prev, "to": req.Mode, "via": caller.Subject})
	writeJSON(w, http.StatusOK, map[string]any{"mode": req.Mode, "previous": prev})
}

func controllersHistory(s *Server, r *http.Request) ([]policy.ActionRecord, map[string]*v1.RemediationAction, error) {
	return controllers.ActionHistory(r.Context(), s.Client, s.SystemNamespace)
}

func (s *Server) decisionWait() time.Duration {
	if s.DecisionWait > 0 {
		return s.DecisionWait
	}
	return 8 * time.Second
}
