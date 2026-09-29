# API reference

AegisOps exposes two HTTP APIs:

- **`aegis-api`** (`http://localhost:8000` in the kind environment) is for humans, the
  dashboard and the CLI. The interactive OpenAPI UI is at `/docs` and the machine-readable
  spec at `/openapi.json`.
- The **control-plane API** (`aegis-controller:8082`, cluster-internal only) is used by
  `aegis-engine` and `aegis-api` with Kubernetes ServiceAccount tokens. See the end of this
  document.

## Conventions

- **Authentication.** `Authorization: Bearer <token>` from `POST /api/v1/auth/login`. Tokens
  expire after 8 hours by default.
- **Errors.** Every error has the same shape:
  ```json
  {"error": {"code": "forbidden", "message": "permission 'approve' required (role viewer)"}, "request_id": "3f9c0d1e2a4b5c6d"}
  ```
  Codes in use: `unauthenticated` (401), `forbidden` (403), `not_found` (404),
  `already_decided` / `invalid_state` / `exists` (409), `validation_error` (422),
  `rate_limited` (429), `controlplane_unavailable` / `prometheus_unavailable` /
  `chaos_unavailable` (502), `internal_error` (500).
- **Request IDs.** Send `X-Request-ID` to correlate. Otherwise the server generates one. It is
  echoed in the response header and error body, and it appears in every log line and
  downstream call.
- **Pagination.** `limit` and `offset` query parameters where lists can grow (incidents, audit).
- **Timestamps** are ISO 8601 in UTC.

## Roles

`viewer` ⊂ `operator` ⊂ `approver` ⊂ `admin`. The permission matrix is in
[SECURITY.md](SECURITY.md#api-roles-aegis-api). The *Permission* column below names what each
endpoint requires.

## Endpoints

### Auth

| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/api/v1/auth/login` | none | `{"username","password"}` → `{"access_token","expires_at","user":{username,role,permissions}}`. Rate-limited per client address |
| GET | `/api/v1/auth/me` | any | Current user and permissions |

### Incidents

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/incidents?status=&limit=&offset=` | read | List. `status=active` or an incident status |
| GET | `/api/v1/incidents/{id}` | read | Incident with latest diagnosis, plan, actions, approvals, simulations, verifications |
| GET | `/api/v1/incidents/{id}/timeline` | read | Every recorded event, in order |
| GET | `/api/v1/incidents/{id}/evidence?kind=` | read | Evidence with provenance, ordered by relevance |
| GET | `/api/v1/incidents/{id}/diagnoses` | read | All diagnosis rounds |
| GET | `/api/v1/incidents/{id}/plans` | read | All remediation plans (every candidate with its policy dry run) |
| GET | `/api/v1/incidents/{id}/snapshots` | read | Full context bundles per round (used by replay) |
| GET | `/api/v1/incidents/{id}/postmortem?format=json\|markdown` | read | Postmortem with typed claims (FACT, HYPOTHESIS, MODEL_INTERPRETATION) |
| POST | `/api/v1/incidents/{id}/investigate` | investigate | Re-open and re-run the investigation (202) |
| POST | `/api/v1/incidents/{id}/resolve` | resolve | `{"note"}`: close manually, including escalated incidents (202) |
| POST | `/api/v1/incidents/{id}/escalate` | resolve | `{"note"}`: stop automation and hand to a human (202) |

### Approvals and actions

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/approvals?status=` | read | Approval requests with full context (candidate, risk, policy reasons, simulation) |
| POST | `/api/v1/approvals/{action_id}/decision` | approve | `{"decision":"approved"\|"rejected","reason"}`. The API re-reads the action, signs over its spec hash, and forwards to the controller. 409 if already decided or no longer awaiting approval |
| GET | `/api/v1/actions?incident=&phase=&limit=` | read | Actions mirrored from the controller |
| GET | `/api/v1/actions/{id}` | read | Action plus the live controller object |
| GET | `/api/v1/simulations?incident=` | read | Sandbox simulations with baseline and candidate metrics |

### Policy

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/policy` | read | Effective policy, its source, circuit-breaker state, action registry |
| POST | `/api/v1/policy/check` | read | Dry run: `{"action_type","target","target_kind","parameters","diagnosis_confidence"}` returns the full decision with every check |
| PUT | `/api/v1/policy/mode` | policy_admin | `{"mode":"observe"\|"supervised"\|"autonomous"}` (HMAC-signed to the controller, audited) |
| GET | `/api/v1/policy/decisions?limit=` | read | Recent decisions with per-check detail |

### Services and topology

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/services` | read | Services with live signals, tier, version, replicas, dependencies |
| GET | `/api/v1/services/{name}` | read | Signals, workload (pods, containers, rollout history), 6h change log |
| GET | `/api/v1/services/{name}/metrics?minutes=` | read | RPS, 5xx ratio, p95, CPU, memory, DB pool series (5–360 min) |
| GET | `/api/v1/changes?hours=` | read | Change log for the managed namespace |
| GET | `/api/v1/topology` | read | Reconciled dependency graph with edge provenance and root-cause candidates |
| GET | `/api/v1/topology/impact/{service}` | read | Blast radius with dependency paths |
| GET | `/api/v1/topology/explain/{service}` | read | Dependencies that could explain a symptom on the service |

### Platform

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/overview` | read | Dashboard summary |
| GET | `/api/v1/system/health` | read | Health of every AegisOps component and backend |
| GET | `/api/v1/system/ai` | read | Model calls: provider, model, status, tokens, cost, latency |
| GET | `/api/v1/events?after=&limit=` | read | Event backlog |
| GET | `/api/v1/events/stream?after=` | read | Server-sent events (see below) |
| GET | `/api/v1/audit?limit=&offset=&source=` | audit | Audit log (`source=aegis` or `controller`) |
| GET | `/api/v1/audit/verify` | audit | Recompute the hash chain: `{"valid","checked","head"}` or `{"valid":false,"broken_at","reason"}` |
| GET | `/healthz`, `/readyz`, `/metrics` | none | Liveness, readiness (database), Prometheus metrics |

### Evaluation

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/evaluations` | read | Benchmark runs with summaries |
| GET | `/api/v1/evaluations/{run_id}` | read | Run metadata, summary, per-scenario results |
| POST | `/api/v1/evaluations` | benchmark | Create a run (used by `aegis benchmark run`) |
| POST | `/api/v1/evaluations/{run_id}/results` | benchmark | Record one scenario result |
| POST | `/api/v1/evaluations/{run_id}/complete` | benchmark | Attach the summary and finish the run |

### Demo (only when `AEGIS_DEMO_MODE=true`)

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/v1/demo/scenarios` | read | Scenario catalogue (ground truth is not exposed) |
| GET | `/api/v1/demo/status` | read | Drift from baseline, active network faults, traffic, canaries |
| POST | `/api/v1/demo/scenarios/{id}/inject` | demo | Inject a fault into the demo namespace (audited; rate-limited to one per 10s) |
| POST | `/api/v1/demo/reset` | demo | Restore golden Deployments and ConfigMaps, clear network faults, reset traffic, delete canaries |

## Server-sent events

```
GET /api/v1/events/stream
Authorization: Bearer <token>
Accept: text/event-stream
```

Each event:

```
id: 4211
event: action_completed
data: {"id":4211,"incident_id":"INC-20260929-DBFD95","ts":"...","type":"action_completed","message":"...","actor":"aegis-controller","data":{...}}
```

New clients start from the present. Reconnect with `Last-Event-ID` or `?after=<id>` to resume
without gaps (events are persisted, so resumption is lossless). A `: keepalive` comment is
sent every 15s. The dashboard reads the stream with `fetch` (so it can send the bearer header)
and revalidates the views each event touches.

## Examples

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<from python scripts/devctl.py credentials>"}' | jq -r .access_token)

# Active incidents
curl -s "localhost:8000/api/v1/incidents?status=active" -H "Authorization: Bearer $TOKEN" | jq '.items[] | {id,status,title}'

# Would policy allow scaling payment-service to 4 replicas right now?
curl -s -X POST localhost:8000/api/v1/policy/check -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action_type":"scale_deployment","target":"payment-service","parameters":{"replicas":4}}' | jq '.decision | {allowed,requiresApproval,riskLevel,reasons}'

# Approve a pending action
curl -s -X POST localhost:8000/api/v1/approvals/<action-id>/decision -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"decision":"approved","reason":"matches the release diff"}'
```

The same operations are available through the CLI (`aegis --help`), for example
`aegis incidents --status active`, `aegis incident get <id>`, `aegis approve <action-id> --reason ...`,
`aegis policy check scale_deployment payment-service --param replicas=4`.

## Control-plane API (internal)

Authenticated with projected ServiceAccount tokens (audience `aegisops-controlplane`),
verified by `TokenReview`. Roles come from the controller's `--role-bindings` flag.
NetworkPolicy allows only the engine and API pods to connect.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/v1/workloads`, `/v1/workloads/{ns}/{name}` | reader | Deployments with pods, containers, rollout history |
| GET | `/v1/topology` | reader | Declared dependency graph |
| GET | `/v1/changes?since=&namespace=&name=` | reader | Change log (template, ConfigMap and scale diffs) |
| GET | `/v1/signals?since=` | reader | Kubernetes events and pod-state transitions |
| GET | `/v1/policy` | reader | Effective policy, breaker, registry |
| POST | `/v1/policy/evaluate` | reader | Policy dry run for a proposed action |
| GET | `/v1/audit?after=&boot=` | reader | Controller audit ring (synced into PostgreSQL by the engine) |
| GET/POST | `/v1/actions` | reader / proposer | List or submit a `RemediationAction` (the response waits briefly for the admission decision) |
| GET | `/v1/actions/{name}?waitForVersion=&timeoutSeconds=` | reader | Long-poll for status changes |
| POST | `/v1/actions/{name}/approval` | approver | Signed decision (`specHash`, `decision`, `approver`, `reason`, `issuedAt`, `signature`) |
| POST | `/v1/actions/{name}/revert` | proposer | Request a `revert_action` from the action's snapshot |
| GET/POST | `/v1/simulations`, GET `/v1/simulations/{name}` | reader / proposer | Sandbox simulations |
| GET | `/v1/canaries` | reader | CanaryRelease status |
| PUT | `/v1/policy/mode` | approver | Signed mode change |
