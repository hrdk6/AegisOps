// Package telemetry holds the controller's Prometheus metrics and OpenTelemetry setup.
package telemetry

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
	// PolicyDecisions counts policy outcomes by action type, decision and risk.
	PolicyDecisions = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "aegis_policy_decisions_total", Help: "Policy decisions by action type, outcome and risk level.",
	}, []string{"action_type", "outcome", "risk"})

	// ActionTransitions counts action phase transitions.
	ActionTransitions = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "aegis_action_transitions_total", Help: "RemediationAction phase transitions.",
	}, []string{"action_type", "phase"})

	// ActionDuration observes execution duration of completed actions.
	ActionDuration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name: "aegis_action_execution_seconds", Help: "Execution time of remediation actions.",
		Buckets: []float64{1, 5, 10, 20, 30, 60, 120, 240},
	}, []string{"action_type", "outcome"})

	// Simulations counts simulation verdicts.
	Simulations = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "aegis_simulations_total", Help: "Sandbox simulations by phase and verdict.",
	}, []string{"phase", "verdict"})

	// CanaryAnalyses counts canary analysis verdicts.
	CanaryAnalyses = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "aegis_canary_analyses_total", Help: "Canary analysis verdicts.",
	}, []string{"verdict"})

	// BreakerOpen reports the circuit breaker state (1=open).
	BreakerOpen = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "aegis_circuit_breaker_open", Help: "1 when the remediation circuit breaker is open.",
	})

	// APIRequests counts control-plane API requests.
	APIRequests = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "aegis_controlplane_api_requests_total", Help: "Control-plane API requests by route, code and caller role.",
	}, []string{"route", "code"})
)

func init() {
	metrics.Registry.MustRegister(PolicyDecisions, ActionTransitions, ActionDuration, Simulations, CanaryAnalyses, BreakerOpen, APIRequests)
}
