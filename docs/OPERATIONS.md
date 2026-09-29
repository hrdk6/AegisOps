# Operations

How to run AegisOps day to day: choosing a mode, handling approvals, reading its decisions,
and what to do when AegisOps itself misbehaves.

## Automation modes

| Mode | Behaviour | Use when |
|---|---|---|
| `observe` | Detects, investigates, diagnoses and plans. **Every action is denied.** | First deployment against a new application; after an AegisOps misjudgement; during freezes |
| `supervised` | Every action needs a signed human approval | Building trust: reviewing proposals before letting LOW risk run |
| `autonomous` | LOW risk runs; MEDIUM runs only with an Improved sandbox simulation, confidence ≥ the autonomy minimum, and a closed breaker; HIGH and CRITICAL always need a human | Steady state |

Change it with `aegis policy mode supervised`, from the Automation page (admin only), or with
`PUT /api/v1/policy/mode`. Every change is HMAC-signed and audited.

Other tunables live in the `AegisPolicy` custom resource `default`
(`deploy/base/aegis/policy.yaml`): allowed namespaces, protected workloads, autonomy minimum
confidence, approval threshold, simulation-gated autonomy, replica, scale and resource limits,
budgets, circuit breaker, per-action rules, approval TTL, execution timeout, and auto-revert.
Edit it with `kubectl edit aegispolicy default`. The controller validates it and reports
`status.observedGeneration`. If the policy object is missing, the controller falls back to a
**fail-closed** built-in default (supervised, no allowed namespaces).

## Approvals

A pending approval appears in the dashboard header, on the incident page and under
Automation → Pending approvals, and in `aegis approvals`. Before approving, check:

1. **Diagnosis**: the selected hypothesis, its confidence, and the cited evidence. Click an
   evidence id to see the exact query that produced it.
2. **Why approval is required**: the policy reasons (risk level, protected workload,
   confidence below autonomy minimum, circuit breaker open, supervised mode).
3. **Simulation**: whether the sandbox showed an improvement, and on which metrics.
4. **Rollback strategy**: what the controller will restore if verification fails.

Approve or reject with a reason. The reason is part of the signed payload and the audit log.
Approvals expire after `approvalTtlSeconds` (15 minutes by default). An expired or rejected
action stops automation for that incident and escalates it. If symptoms clear while waiting,
the request is marked *superseded* and the incident closes as self-recovered.

## Reading an incident

The incident page follows the loop: **Detect → Investigate → Diagnose → Simulate → Authorize →
Execute → Verify → Resolved or Escalated**. Each stage has a timestamp. A dashed stage was
skipped, for example no simulation for a scale action.

- **Diagnosis** lists every hypothesis with its confidence, its source (deterministic rules,
  model, or both), and supporting (+) and contradicting (−) evidence. The *Uncertainty* line
  says what the system could not see.
- **Remediation candidates** shows every option considered, its policy dry run, P(success),
  and utility, not only the chosen one.
- **Actions and verification** shows every policy check behind the decision and the
  before/after SLO table from verification.
- **Timeline** is the authoritative record. *Show every event* includes status transitions
  and raw anomaly events.
- The **postmortem** (generated when the incident closes) labels every statement as FACT,
  HYPOTHESIS, or MODEL INTERPRETATION, with evidence references. Download it as Markdown.

## Operator actions on incidents

| Action | CLI | Effect |
|---|---|---|
| Re-investigate | `aegis incident investigate <id>` | Re-opens a closed incident and runs a fresh investigation round |
| Escalate | `aegis incident escalate <id>` | Cancels the workflow; the incident becomes human-owned |
| Resolve | `aegis incident resolve <id>` | Closes the incident (including escalated ones) as `resolved_manually` |

While an escalated incident's symptoms continue, new anomalies on its services attach to it
rather than opening new incidents. A 45s symptom-free gap ends that.

## When AegisOps itself has problems

| Symptom | Likely cause | What to do |
|---|---|---|
| Header shows **Circuit breaker: open** | ≥ 4 failed or reverted actions within 15 minutes | Every action now needs approval (by design). Review the failed actions under Automation → Recent policy decisions. The breaker closes on its own after 5 minutes without new failures |
| System health: `engine` down or stale heartbeat | Engine crash or restart | `kubectl -n aegis-system logs deploy/aegis-engine`. On restart the engine resumes open incidents from investigation and expires approvals it can no longer track |
| System health: `prometheus`, `loki` or `jaeger` degraded | Backend down | Detection continues on the remaining signals; investigations record *gaps* and lower confidence. Fix the backend; no AegisOps restart is needed |
| System health: `controller` down | Control plane unavailable | The engine cannot evaluate policy, so every candidate is treated as not permitted (fail closed) and incidents escalate. Check `kubectl -n aegis-system logs deploy/aegis-controller` |
| Actions denied with `preconditions: stale plan` | The target was rolled out after the plan was made | Expected safety behaviour. The next round re-plans against the current revision |
| Many incidents titled "Latency SLO breach" right after resets or rollouts | Transient latency from pod restarts | These usually close as *self_recovered*. Tune `p95_ms` in `config/slos.yaml`, or `AEGIS_DETECTION_PERSISTENCE`, if they are noise in your environment |
| Audit verification fails | Database tampering or corruption | `aegis audit verify` reports the first broken entry. Entries after it cannot be trusted. Investigate database access |
| Simulations are Inconclusive | The probe sent < 20 requests (wrong probe path, crash-looping clone) | Check `aegisops.io/simulation-probe` on the Deployment, and `kubectl -n aegis-sandbox get pods,jobs` during a simulation |

## Observability

- **Grafana** (http://localhost:3001, user `admin`, password from `devctl.py credentials`)
  has provisioned Prometheus, Loki and Jaeger data sources.
- Useful metrics:
  - engine: `aegis_engine_detection_cycle_seconds`, `aegis_active_anomalies{service,signal}`,
    `aegis_service_status`, `aegis_incidents_created_total{severity}`;
  - model router: `aegis_llm_calls_total`, `aegis_llm_tokens_total`,
    `aegis_llm_cost_usd_total`, `aegis_llm_latency_seconds`;
  - controller: `aegis_policy_decisions_total`, `aegis_action_transitions_total`,
    `aegis_action_execution_seconds`, `aegis_circuit_breaker_open`, `aegis_simulations_total`,
    `aegis_canary_analyses_total`, `aegis_controlplane_api_requests_total`;
  - ShopFlow: `shop_http_requests_total`, `shop_http_request_duration_seconds` and
    client-side dependency metrics.
- Traces: search Jaeger for service `aegis-engine` (operations `incident.investigate`,
  `incident.execute`, `incident.verify`) or `aegis-controller`.
- Logs are JSON with `request_id` and `incident_id`. In Loki, query
  `{namespace="aegis-system"} | json | incident_id="INC-..."`.

## Backup and retention

Incident state and the audit log are in `aegis-postgres` (PVC `aegis-postgres-data`). For
anything beyond the demo, schedule `pg_dump` and ship the audit-chain head (the `head` field
printed by `aegis audit verify`) to write-once storage. `RemediationAction` and
`RemediationSimulation` objects accumulate in `aegis-system`. There is no TTL controller yet;
delete old ones with `kubectl delete remediationactions -n aegis-system --field-selector ...`
or by label after exporting.

## Upgrading

1. Switch to `supervised` (or `observe`).
2. Wait for in-flight actions to finish: `kubectl get remediationactions -n aegis-system`.
3. Apply CRDs, then images: `python scripts/devctl.py build ... && kubectl apply -k deploy/overlays/dev`.
   Database migrations run automatically in the `aegis-api` init container.
4. Check System health, then restore the previous mode.

## Resetting the demo environment

`aegis scenario reset` (or the Demo page) restores golden Deployments and ConfigMaps, removes
network faults and canaries, and resets traffic. It is refused unless the namespace carries
`aegisops.io/chaos-enabled=true`. A reset rolls pods, so expect short, self-recovering latency
incidents afterwards.
