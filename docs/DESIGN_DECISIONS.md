# Design decisions

Each entry records the decision, the alternatives considered, why this option won, and what
it costs. Several entries came out of failures found while running the system live; those
are marked **(found in testing)**.

---

### D1. Separate the reasoning layer (Python) from the execution layer (Go controller)

**Decision.** All cluster mutations go through a Go controller with its own API, CRDs, policy
engine and RBAC. The Python engine has no Kubernetes credentials.

**Alternatives.** (a) One Python service using the Kubernetes client directly. (b) Shelling
out to `kubectl`. (c) An operator framework in Python (kopf).

**Why.** The component that reads untrusted telemetry and model output should not hold
cluster authority. A separate process with a narrow typed API makes that boundary
enforceable: RBAC per ServiceAccount, audience-bound tokens, and the approval key held
elsewhere. Go and controller-runtime give watches, optimistic concurrency, status
subresources and leader election as first-class features. `kubectl` would reintroduce string
commands and kubeconfig handling, which is exactly what the safety model forbids.

**Cost.** Two languages. Hashing and signing formats must stay identical (a shared test vector
guards this). There is an extra network hop per action.

### D2. Actions are CRDs (`RemediationAction`) with immutable specs

**Decision.** A proposal is a Kubernetes object. Policy decision, approval, snapshot and
outcome are written to its status. A CEL rule makes the spec immutable.

**Alternatives.** An in-memory queue, or rows only in PostgreSQL.

**Why.** The object is durable across controller restarts, observable with standard tools,
and naturally idempotent (the reconciler resumes an interrupted execution). Immutability
means an approval binds to exactly what will run. The engine mirrors each action into
PostgreSQL for history and UI queries.

**Cost.** Etcd holds action history (bounded in a real deployment by TTL cleanup, which is not
implemented).

### D3. A deterministic policy engine written as a pure Go function, not OPA/Rego

**Decision.** `policy.Evaluate(Input) Decision` with unit tests per rule. Tunables (mode,
budgets, limits, protected workloads, per-action rules) live in the `AegisPolicy` CR.

**Alternatives.** OPA/Gatekeeper with Rego; Kyverno; a policy DSL.

**Why.** Rules need typed state (history, simulation verdicts, rollout revisions, breaker
state) that is awkward to marshal into Rego inputs. A pure function is trivially testable,
has no extra runtime, and produces an explanation (per-check pass/fail) that the UI shows
verbatim.

**Cost.** Adding a new *kind* of rule needs a code change. Operators can tune but not program
policy.

### D4. Rules-first diagnosis; models are optional and bounded

**Decision.** Ten deterministic rules produce evidence-cited hypotheses. A model, when
configured, can re-rank and add hypotheses under grounding validation. Fusion is
`0.6·rules + 0.4·model`, and model-only hypotheses are capped.

**Alternatives.** LLM-first diagnosis with retrieval; a pure ML classifier.

**Why.** The system must work, and be evaluated, with no API keys. Deterministic rules are
reproducible (benchmark replay gives identical results), explainable, and cannot be
prompt-injected. Models add value on phrasing and unusual combinations; bounding their weight
limits the damage when they are wrong.

**Cost.** Rules cover the failure mechanisms we wrote rules for. Novel failure modes fall to
`UNKNOWN` and escalate, which is intended but reduces autonomy.

### D5. Calibrate with a tempered softmax that includes an explicit UNKNOWN

**Decision.** Rule scores become confidences via `softmax(score / 2)` over the hypotheses
*plus* an `UNKNOWN` pseudo-hypothesis with score 2.0. Selection needs ≥ 0.35 and a
non-UNKNOWN winner.

**Why.** Raw rule scores are unbounded, and normalising over the candidates alone always
produces a confident winner, even with one weak rule firing. The UNKNOWN mass makes weak
evidence produce low confidence. Calibration is measured in the benchmark (Brier score).

**Cost.** The constants are hand-set. They should be refit from benchmark data at scale.

### D6. Bounded re-investigation before escalating **(found in testing)**

**Decision.** If the first diagnosis is inconclusive, collect again after three detector cycles,
at most twice, before escalating. Anomalies that are still unconfirmed are also passed to the
context engine.

**Why.** In live runs, incidents were opened on the first confirmed symptom (for example the
gateway's 5xx ratio) before the root service's own anomaly had persisted long enough. The
first diagnosis then escalated prematurely.

**Cost.** Up to ~30s more before escalating a genuinely unknown incident.

### D7. Stale-plan protection with `expectedRevision` **(found in testing)**

**Decision.** Every Deployment action carries the rollout revision it was planned against.
Policy checks it at admission and at the post-approval recheck; the executor checks it again
atomically, inside the optimistic-concurrency update.

**Why.** An environment reset happened between diagnosis and execution in one live run, so
"roll back to the previous revision" rolled back *to the bad release*. The verifier caught it
and reverted, but the action should never have run. Rollbacks now also carry an explicit
`toRevision`.

**Cost.** A legitimate action can be refused if an unrelated rollout lands in between. The
engine then re-plans in the next round.

### D8. PostgreSQL for everything; full-text search instead of pgvector

**Decision.** One PostgreSQL database for incidents, evidence, audit, runbooks and
evaluations. Runbooks are retrieved by cause category plus `tsvector` rank.

**Alternatives.** pgvector embeddings; a dedicated vector database; Elasticsearch.

**Why.** The runbook corpus is small (nine documents) and category metadata is exact, so
lexical ranking inside a category is sufficient. It is deterministic (replayable) and needs
no embedding model or API key. pgvector would add an embedding dependency with no measured
retrieval gain at this size.

**Cost.** Poor recall for paraphrased queries. Revisit if the corpus grows to hundreds of
documents.

### D9. PostgreSQL `LISTEN/NOTIFY` for events and commands, and SSE to browsers

**Alternatives.** Kafka, NATS, Redis streams; WebSockets.

**Why.** There are two publishers and a handful of subscribers, and every event is already
written to the `events` table (durable; SSE resume uses `?after=<id>`). A trigger-based
NOTIFY adds real-time fan-out with no new infrastructure. SSE is one-way, proxy-friendly, and
the client reconnects with backoff.

**Cost.** NOTIFY payloads are small, and delivery to disconnected listeners is covered by the
table, not by the channel.

### D10. Replica-ratio canaries instead of a service mesh

**Decision.** `CanaryRelease` runs a canary Deployment behind the stable Service and steps its
replica share, with a Prometheus analysis per `pod-template-hash`.

**Alternatives.** Argo Rollouts; Flagger with Istio or Linkerd traffic splitting.

**Why.** A mesh roughly doubles the local footprint and complexity. The replica-ratio model is
the same "basic canary" that Argo Rollouts uses without a mesh. It is enough to demonstrate
progressive delivery with automated abort, and it integrates with the policy (`abort_canary`,
`pause_rollout`).

**Cost.** Traffic weights are approximate (replica ratio × load balancing) and cannot go below
1/N.

### D11. An in-cluster digital twin for simulation

**Decision.** Clone the current spec and the candidate spec into an isolated namespace with
data-store sidecars, and drive both with identical synthetic load.

**Alternatives.** Static analysis of the diff; a full staging environment; replaying
production traffic.

**Why.** For code and configuration faults, which make up most of the benchmark, running the
actual image with the actual configuration is the most faithful cheap test. It takes about
50s, uses no production data, and is deterministic enough to gate MEDIUM-risk autonomy.

**Cost.** It cannot model network faults, load mix, data-dependent bugs or cross-service
interactions. "No simulation" is therefore neutral in policy, never a pass.

### D12. A purpose-built TCP fault proxy instead of Toxiproxy **(found in testing)**

**Decision.** `shopflow netem` is a small asyncio TCP proxy with latency, reset-peer and
timeout faults, and an HTTP control API.

**Why.** Under sustained load in this environment, Toxiproxy repeatedly wedged: connections
stayed stuck after toxics were removed, so network scenarios could not be reset reliably.
The replacement is about 150 lines, exposes Prometheus metrics, and resets cleanly.

**Cost.** Fewer fault types than Toxiproxy (no bandwidth or slicer faults).

### D13. Verification by before/after SLO checks, then revert on no effect or degradation

**Decision.** Wait for sustained health (four clean detector cycles) or a timeout. Compare
per-service checks and a normalised violation score. NO_EFFECT and DEGRADED revert the action,
PARTIAL keeps it, and RESOLVED starts a 60s recurrence watch.

**Why.** "The rollout completed" is not "the incident is fixed". An action that did nothing
still has a cost (a changed system), so it is undone. The recurrence watch catches fixes that
only masked a symptom.

**Cost.** Transient noise during the verification window can cause a false NO_EFFECT and an
unnecessary revert. The settle period and streak requirement reduce this.

### D14. Escalated incidents absorb recurring anomalies while symptoms persist **(found in testing)**

**Why.** Without this, an escalated but still-ongoing problem produced a new incident about
every 75 seconds (merge window → escalate → new incident), flooding the on-call. A 45s
symptom-free gap ends the hold, so a genuinely new fault still opens a new incident.

### D15. kind with a Python `devctl` script instead of Terraform, Helm or Tilt

**Decision.** A stdlib-only Python script creates the cluster, builds and loads images,
generates secrets and applies Kustomize manifests. It runs the same way on Windows, macOS and
Linux.

**Why.** Terraform adds most value for cloud resources and shared state. For one local kind
cluster it would add a provider dependency and state files without replacing the image
build/load steps. Kustomize is built into kubectl. We chose not to add Terraform rather than
add it for appearance. A cloud deployment would warrant it (see the roadmap in the README).

**Cost.** No declarative drift detection for the cluster itself. `devctl up` is idempotent
instead.

### D16. Benchmark ground truth is hidden from the engine

**Decision.** Scenario definitions (fault, expected root cause, acceptable/optimal/prohibited
actions, expected outcomes) are read only by the chaos injector and the scorer. The engine
sees telemetry only.

**Why.** Otherwise the benchmark measures the scenario file, not the system. Rules are written
against mechanisms (a change followed by errors, OOMKilled, pool saturation) and never
against scenario-specific flags or service names.

**Cost.** Some scenarios are ambiguous by design (a release that breaks configuration is both
BAD_DEPLOYMENT and CONFIG_ERROR). The ground truth lists every acceptable category.

---

The following decisions were forced by the first complete live benchmark run
(`live-20260929-010108-efd4`). Each has a regression test named after the failure.

### D17. Controller progress checks judge only what the action created **(found in the benchmark)**

**Decision.** A rollout counts as failed only if one of these holds:

- a `ProgressDeadlineExceeded` condition was updated *after* the action started;
- a pod of the ReplicaSet for the Deployment's *current* revision, created after the action
  started, is crash-looping. This is checked only once the new spec has been observed.

**Why.** Two remediations that were correct were marked failed within a second of starting.
Rolling back or restoring resources re-activates an *older* ReplicaSet. The previous check
took the most recently *created* ReplicaSet (the faulty one) and saw its crashing pods. In
another case, the Deployment still carried the deadline condition from the bad release. The
false failures triggered automatic reverts, which put the fault back.

**Cost.** A genuinely stuck rollout is caught by the execution timeout rather than instantly
by a stale condition.

### D18. `update_config` pins the concrete revision when the snapshot is taken **(found in the benchmark)**

**Decision.** `configRevision: previous` is resolved to a content hash once, before applying,
and stored in the action's snapshot. Re-applies use the pinned hash.

**Why.** "Previous" was resolved at apply time relative to a history that the action's own
write extends. When the reconciler re-applied (normal crash-safe behaviour), "previous" had
become the faulty configuration. The recorded history showed the fix at `01:17:41.427` and the
fault written back 36 ms later.

### D19. A revert that restores an already-unhealthy state is not a failed revert

**Decision.** Deployment snapshots record ready replicas. A revert whose restored state was
already unhealthy before the reverted action is reported as *restored, still unhealthy*, and
the incident escalates. It is no longer reported as a revert failure.

**Why.** The revert did exactly its job: it put back the pre-action state. Reporting it as
failed made the benchmark's "rollback success" metric (0/2 in run 1) misleading, and
suggested the controller had left the system in an unknown state.

### D20. Diagnosis rules use weaker corroborating signals than alerting does **(found in the benchmark)**

**Decision.** Five mechanism-level refinements:

- Heavy CFS throttling (≥ 25% of periods) plus the service's own latency anomaly counts as
  CPU saturation, even when *average* utilisation is moderate. Bursty load hits the limit.
- A CPU-throttled caller's client-side latency is discounted as network evidence, because a
  starved process inflates every outbound timing.
- A resource change counts as misconfiguration only if something was *lowered*.
- A change that re-activated a ReplicaSet which had run before, followed by more than 90s of
  health, is treated as a rollback or reset, not as a regression-introducing release.
- Connection-level transport errors (resets, unreachable, protocol errors; *not* read
  timeouts, which starved services also produce) support a network hypothesis at a lower
  client error ratio, but only if the dependency itself looks healthy.

**Why.** Three of the five run-1 failures blamed the *previous* scenario's cleanup (a rollback
or a resource reset) for an unrelated fault. The fourth missed a CPU-bound service whose
average utilisation stayed under the paging threshold. Alert thresholds are tuned to avoid
paging on noise. Diagnosis, which only runs once something is already wrong, can afford
weaker signals when they agree.

**Cost and caveat.** These rules were refined on run 1's evidence. Replaying run 1's captured
bundles afterwards (19/19 top-1) is therefore a development-set number, not validation. The
fresh live run reported in [EVALUATION.md](EVALUATION.md) is the relevant test.

### D21. Verification judges SLOs, not early-warning heuristics **(found in the benchmark, run 2)**

**Decision.** The "no active anomalies" verification check, and the healthy streak the
verifier waits for, ignore a latency flag raised only because p95 is several times its
historical baseline while still inside the SLO. That flag is reported as advisory.

**Why.** In the traffic-surge scenario, one scale-up brought every SLO back (violation score
0.17 → 0.00). Latency legitimately settled above the pre-surge baseline, so the flag stayed
on. Verification reported PARTIAL, the engine scaled again, measured NO_EFFECT, reverted,
and escalated an incident that had been fixed.

**Cost.** A regression that stays under the SLO is no longer grounds for "not resolved". It
still opens incidents: the detector keeps the baseline heuristic for detection.
