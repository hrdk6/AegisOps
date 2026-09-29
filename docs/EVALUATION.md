# Evaluation

AegisOps is evaluated by injecting controlled faults into the running environment, letting
the full system handle each one end to end, and scoring what it did against ground truth it
never sees. This page covers the methodology, the metrics, how to reproduce a run, and the
results we observed. Every number in the results section comes from a recorded run in
`benchmarks/results/<run_id>/`; nothing is estimated or hand-entered.

## Method

**Live mode** (`aegis benchmark run`). For each scenario and repetition, the runner:

1. closes any incident left over from the previous scenario, resets the demo environment
   (golden Deployments and ConfigMaps, no network faults, default traffic, no canaries), then
   waits for **60 seconds of sustained health** with no open incident;
2. injects the fault through the chaos API and records the injection time;
3. waits for an incident opened after the injection (per-scenario detection timeout), then
   follows it to a terminal state (resolution timeout), approving any approval request as a
   stand-in human (`benchmark approver`; each approval is counted as a human intervention);
4. waits for the postmortem, then downloads the incident, all evidence and the telemetry
   snapshots;
5. scores the observation against the scenario's ground truth (`evaluation/scoring.py`).

The engine is not told a benchmark is running. Scenario files are read only by the chaos
injector and the scorer. The engine's rules reason about mechanisms and never reference
scenario flags or service names (reviewed in code; see D16 in
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)).

**Replay mode** (`aegis benchmark replay <run_dir> --config name=<routes>`) re-runs the
diagnosis engine on the *exact* evidence bundles captured during a live run, under different
configurations (rules only, rules plus a given model route). Inputs are identical, so
differences in accuracy, calibration, latency and cost are attributable to the configuration
and not to environmental noise. This is how a model provider should be compared.

## Metrics

| Metric | Definition | Denominator |
|---|---|---|
| Pass rate | detection matches expectation **and** root cause correct (when detected and defined) **and** outcome acceptable **and** no unsafe or false remediation | all runs |
| Detection rate | an incident was opened after injection | runs whose expected outcome requires an incident |
| Root-cause accuracy (top-1 / top-3) | selected (or any of the top 3) hypothesis has an acceptable category **and** the right component | detected runs with a defined root cause |
| Remediation success | outcome `resolved`, at least one action executed, no unsafe action | scenarios whose only acceptable outcome is `resolved` |
| First action acceptable | the first executed action is in the scenario's acceptable list | runs with ≥ 1 executed action |
| Unsafe-action rate | any executed action is in the scenario's *prohibited* list | all runs |
| False-remediation rate | any executed action is outside the *acceptable* list | all runs |
| Outcome match | final outcome is one of the scenario's acceptable outcomes | all runs |
| Rollback success | every revert AegisOps issued succeeded | runs with ≥ 1 revert |
| Human intervention rate | at least one approval or manual decision was needed | all runs |
| Time to detect | injection → incident creation (includes the fault's own rollout time) | detected runs |
| Detection latency | first anomalous sample (incident onset) → incident creation | detected runs |
| Recovery time | injection → verified resolution | resolved runs |
| MTTR | detection → verified resolution | resolved runs |
| Actions per incident | executed actions, excluding reverts | detected runs |
| Evidence grounding | share of evidence ids cited by the selected hypothesis that exist in the stored evidence | runs with citations |
| Calibration (Brier) | mean (confidence − correct)² of the selected hypothesis | detected runs with a defined root cause |

**Uncertainty.** Rates are reported with 95% **Wilson score** intervals, which remain valid
for small n and for rates at 0% or 100%. Durations are reported as median, mean and p90, with
a 95% percentile-bootstrap interval of the mean (2,000 resamples, fixed seed). With one
repetition of 21 scenarios, intervals are wide. That width is the honest statement of what one
run supports. Use `--repeat N` for tighter estimates.

**Reproducibility metadata** is stored with every run: git commit (or `uncommitted`) and dirty
flag, a content hash of all source, configuration, scenario and manifest files, the engine's
configuration fingerprint, a hash of the effective policy, the policy mode, the model routes
in use, the repetition count, and start and finish times.

## Reproducing

```bash
python scripts/devctl.py up                      # environment
export AEGIS_API_URL=http://localhost:8000
aegis/.venv/bin/aegis login --username admin
aegis/.venv/bin/aegis benchmark run              # all 21 scenarios, 1 repetition (about 80–90 minutes)
aegis/.venv/bin/aegis benchmark run --scenario payment-bad-release --repeat 5
aegis/.venv/bin/aegis benchmark replay benchmarks/results/<run_id> \
    --config 'gpt={"diagnosis":["openai:gpt-4.1-mini","heuristic"]}'   # needs OPENAI_API_KEY where replay runs
```

Each run writes `benchmarks/results/<run_id>/` with one JSON file per scenario (the full
incident, evidence and snapshots), `summary.json`, and `report.md`. Summaries also appear on
the dashboard's Evaluation page.

## Threats to validity

- **Same authors wrote the faults and the rules.** The rules target generic mechanisms, but
  their coverage reflects failure modes we anticipated. Novel mechanisms should fall to
  `UNKNOWN` and escalate, and the benchmark cannot show how often that happens in the wild.
- **Synthetic traffic and a single node.** Load is steady and synthetic. Noisy neighbours,
  diurnal load and multi-node scheduling effects are absent.
- **One environment, sequential runs.** Scenarios run back to back in one cluster. The
  60-second stabilisation reduces carry-over but cannot rule it out.
- **The benchmark approver approves everything.** This measures proposal quality under
  approval, not human judgement.
- **No model in the reported configuration.** Unless stated otherwise, results are for the
  deterministic path. Model-assisted configurations should be compared with replay.

## Results

All runs below use the deterministic path (no model provider configured), policy mode
`autonomous`, 21 scenarios × 1 repetition, and the benchmark approver. Raw per-scenario JSON
(full incident, evidence and telemetry snapshots), `summary.json` and `report.md` are in
`benchmarks/results/<run_id>/`. That directory is git-ignored, so publish runs deliberately.

### How we got to a complete run

Three earlier attempts were stopped partway and are kept in the database as `failed` runs,
each with its reason. Each exposed a real defect, fixed before the next attempt:

| Attempt | Stopped because | Fix |
|---|---|---|
| 1 | The runner's stabilisation step closed every active incident every 5s, while the engine re-opened one each cycle for a still-transient symptom | Close leftovers once before resetting; afterwards let the engine handle reset-induced incidents |
| 2 | A rollback was marked failed in the same second it started: the Deployment still carried the previous rollout's `ProgressDeadlineExceeded` condition | Ignore deadline conditions last updated before the action started (D17) |
| 3 | One PromQL join failed (duplicate cAdvisor series during a container restart), which disabled *all* metric signals and read missing traffic as "0 req/s" | Isolate failures per query; aggregate both join sides; treat missing metrics as unknown, not zero |

### Run 1: `live-20260929-010108-efd4` (first complete run)

82 minutes (01:01–02:23 UTC). Source hash `2bf1aff5755ff12a`, policy hash `2ffc5a30cbd067ef`.

| Metric | Result |
|---|---|
| Scenario pass rate | 76% (16/21, 95% CI 55%–89%) |
| Detection rate | 95% (19/20, 95% CI 76%–99%) † |
| Root-cause accuracy (top-1) | 84% (16/19, 95% CI 62%–94%) |
| Root-cause accuracy (top-3) | 89% (17/19, 95% CI 69%–97%) |
| Remediation success (resolvable scenarios) | 80% (12/15, 95% CI 55%–93%) |
| First action acceptable | 100% (14/14, 95% CI 78%–100%) |
| Unsafe-action rate | 0% (0/21, 95% CI 0%–15%) |
| False-remediation rate | 0% (0/21, 95% CI 0%–15%) |
| Outcome matches expectation | 86% (18/21, 95% CI 65%–95%) |
| Rollback success | 0% (0/2, 95% CI 0%–66%) ‡ |
| Human intervention rate | 33% (7/21, 95% CI 17%–55%) |
| Time to detect (injection → incident) | median 13.6s, mean 27.0s, p90 74.3s (n=19) |
| Detection latency (anomaly onset → incident) | median 5.0s, mean 3.8s (n=19) § |
| Recovery time (injection → verified resolution) | median 163.4s, mean 147.5s, p90 239.4s (n=12, 95% CI of mean 106.9–185.7s) |
| MTTR (detection → verified resolution) | median 122.5s, mean 110.8s, p90 165.0s (n=12) |
| Actions per detected incident | mean 0.7 (n=19) |
| Evidence grounding | 100% of cited evidence ids exist (n=19) |
| Calibration (Brier) | 0.126 |

† The one "missed" detection is the canary scenario, where progressive delivery aborted the
canary on its own before any service-level anomaly appeared. That is an acceptable outcome,
and the scenario passed; the detection metric counts it as a miss because the scenario also
permits a detected-and-resolved path.
‡ Both reverts "failed" because they correctly restored a state that was already broken
(see D19). They were consequences of the false failures below, not independent revert bugs.
§ Detection latency is dominated by the 2-cycle persistence requirement (5s cycles). Time to
detect also includes the fault's own rollout time (for example, a release rolling out pods,
or a leak taking time to exhaust a pool).

**Failures, unfiltered**

| Scenario | What happened | Cause inside AegisOps | Status |
|---|---|---|---|
| `auth-invalid-configmap` | Correct diagnosis (CONFIG_ERROR on auth-service), correct action (`update_config`), sandbox *Improved*, approved; then the service crash-looped again and the incident escalated | `update_config` re-resolved "previous" on a crash-safe re-apply and wrote the faulty config back 36 ms after fixing it | Fixed (D18) |
| `inventory-memory-limit-too-low` | Correct diagnosis and action (`patch_resources`); the action was marked failed immediately and reverted | The crash-loop check inspected the *old*, faulty ReplicaSet because the fix re-activated an older one | Fixed (D17) |
| `payment-traffic-surge` | Escalated; blamed a rollback of order-service made 365s earlier | CPU saturation hidden under the paging threshold (43% average, 40% of periods throttled); a rollback treated as a release | Fixed (D20) |
| `order-db-network-latency` | Escalated (correct outcome) but diagnosis blamed the same earlier rollback | As above | Fixed (D20) |
| `order-payment-packet-loss` | Escalated (correct outcome) but diagnosis blamed a resource *increase* from the previous scenario's reset | Resource rule ignored direction; transport errors at 4.3% were below the network rule's threshold | Fixed (D20) |

Three of five failures share one root cause: changes made by the *previous* scenario's
cleanup (a rollback, a resource reset) were weighed as if they were new releases. That
carry-over could only surface when scenarios ran back to back.

**The safety layers did their job.** No unsafe or unacceptable action was executed. In four
incidents the digital twin measured a candidate remedy as **Regressed**, so it was never
submitted. Most of these candidates came from wrong runner-up hypotheses:

| Incident | Candidate blocked by the sandbox | Sandbox measurement |
|---|---|---|
| order-db-network-latency | `rollback_deployment` order-service (back to the leaky release) | errors 0% → 52.4%, p95 23ms → 2014ms |
| payment-traffic-surge | `rollback_deployment` order-service | errors 0% → 35.6%, p95 18ms → 2014ms |
| order-payment-packet-loss | `rollback_deployment` and `patch_resources` on order-service | p95 23ms → 188ms and 174ms |
| redis-network-partition | `update_config` auth-service ("previous" was a broken config from an earlier scenario) | errors 0% → 100% |

In two other incidents (gateway routing, inventory memory limit) the sandbox result was
*NoImprovement*. The probe could not reproduce the fault, which needed specific routes or
more memory growth. MEDIUM risk without an Improved simulation required human approval,
exactly as designed.

**Approvals.** Seven incidents needed a human:

- three had diagnosis confidence below the autonomy minimum of 70% (58%, 65%, 67%);
- three were MEDIUM risk without an Improved simulation (two NoImprovement, plus scaling
  payment back from zero, which is not simulatable);
- one targeted a protected workload (redis).

### Rule changes checked by replay (development set, not validation)

After the fixes, `aegis benchmark replay` re-ran diagnosis on run 1's 19 captured evidence
bundles. Before any change, replay reproduced the live result exactly (top-1 16/19), which
confirms that replay is faithful.

| Rules | Top-1 | Top-3 | Brier |
|---|---|---|---|
| As in run 1 | 84% (16/19) | 89% (17/19) | 0.122 |
| With D20 refinements | 100% (19/19, 95% CI 83%–100%) | 100% (19/19) | 0.059 |

The refinements were designed while looking at these same bundles, so the second row
overstates what to expect on new data. A regression test pins each refinement, including
"near miss" guards such as a genuinely recent release still being blamed. The fresh run below
is the relevant measurement.

### Run 2: `live-20260929-042817-74ac` (fresh run after the run-1 fixes)

76 minutes (04:28–05:44 UTC). Source hash `3bc44e172d224961`. This run used new injections
and newly captured evidence, so its diagnosis numbers are not the replayed development set.
It is still the *same 21 scenarios* whose run-1 evidence shaped the fixes, so it is not an
independent test set either.

| Metric | Run 1 | Run 2 |
|---|---|---|
| Scenario pass rate | 76% (16/21, CI 55–89%) | **95% (20/21, CI 77–99%)** |
| Root-cause accuracy (top-1) | 84% (16/19) | **100% (19/19, CI 83–100%)** |
| Root-cause accuracy (top-3) | 89% (17/19) | 100% (19/19) |
| Remediation success (resolvable) | 80% (12/15) | 93% (14/15, CI 70–99%) |
| First action acceptable | 100% (14/14) | 100% (15/15) |
| Unsafe-action rate | 0% (0/21) | 0% (0/21, CI 0–15%) |
| False-remediation rate | 0% (0/21) | 0% (0/21, CI 0–15%) |
| Outcome matches expectation | 86% (18/21) | 95% (20/21) |
| Rollback success | 0/2 (see D19) | 1/1 |
| Human intervention rate | 33% (7/21) | 43% (9/21, CI 24–63%) |
| Time to detect, median | 13.6s | 12.0s (mean 24.8s, p90 76.3s) |
| Recovery time, median | 163.4s | 156.0s (mean 141.4s, p90 216.9s, n=14) |
| MTTR, median | 122.5s | 112.5s (mean 111.8s, n=14) |
| Calibration (Brier) | 0.126 | 0.052 |

Every run-1 failure passed in run 2: `auth-invalid-configmap`,
`inventory-memory-limit-too-low`, `order-db-network-latency` and `order-payment-packet-loss`.
The traffic surge was now correctly diagnosed (CPU saturation on payment-service) but still
failed on outcome, for a new reason. The first scale-up brought every SLO back (violation
score 0.17 → 0.00), but latency settled above its pre-surge baseline. Verification kept the
incident at PARTIAL, the engine scaled again, measured NO_EFFECT, reverted that second scale,
and escalated. Fixed afterwards (D21, with a regression test). Run 3 below uses that fix.

Human interventions rose from 7 to 9:

- three diagnoses had confidence of 61–66%, below the 70% autonomy minimum;
- three were MEDIUM-risk fixes whose sandbox result was NoImprovement: the probe traffic
  cannot reproduce broken routing or memory exhaustion;
- one scaled payment back from zero (MEDIUM, not simulatable);
- one was the traffic surge's first round, at 44% confidence;
- one targeted redis, a protected workload.

That trade-off is deliberate: the autonomy threshold and the simulation gate hand
borderline cases to a human instead of acting on them. The sandbox again blocked two wrong
candidates: a rollback of order-service during a database-path fault, and restoring
auth-service's "previous" configuration, which was itself a broken revision left by an
earlier scenario.

### Run 3: `live-20260929-054700-a3fa` (the delivered code)

73 minutes (05:47–07:00 UTC). Git `603dad2` plus uncommitted changes (dirty). Source hash
`80b6bcd15469c544`. Policy hash `2ffc5a30cbd067ef`. Deterministic path, no model. The only
code change after this run is the wording of one hypothesis statement: the dependency-failure
text no longer prints "n/a" when no caller is symptomatic. There is no change to scores,
selection or execution.

| Metric | Result |
|---|---|
| Scenario pass rate | **100% (21/21, 95% CI 85%–100%)** |
| Detection rate | 100% (20/20, 95% CI 84%–100%) |
| Root-cause accuracy (top-1) | 100% (20/20, 95% CI 84%–100%) |
| Root-cause accuracy (top-3) | 100% (20/20) |
| Remediation success (resolvable scenarios) | 100% (15/15, 95% CI 80%–100%) |
| First action acceptable | 100% (15/15) |
| Unsafe-action rate | 0% (0/21, 95% CI 0%–15%) |
| False-remediation rate | 0% (0/21, 95% CI 0%–15%) |
| Outcome matches expectation | 100% (21/21) |
| Human intervention rate | 38% (8/21, 95% CI 21%–59%) |
| Time to detect (injection → incident) | median 15.9s, mean 25.9s, p90 78.3s (n=20) |
| Detection latency (anomaly onset → incident) | median 5.0s, mean 3.3s (n=20) |
| Recovery time (injection → verified resolution) | median 139.9s, mean 135.3s, p90 213.2s (n=15, 95% CI of mean 107.5–165.5s) |
| MTTR (detection → verified resolution) | median 105.0s, mean 107.3s, p90 175.0s (n=15) |
| Actions per detected incident | mean 0.8 |
| Evidence grounding | 100% of cited ids exist |
| Calibration (Brier) | 0.053 |

**How the 21 incidents ended**

- **15 resolved.** Seven were fully autonomous. Eight needed a human approval:
  - three had diagnosis confidence of 52–64%, below the 70% autonomy minimum;
  - three were MEDIUM risk with a sandbox result of NoImprovement;
  - one scaled payment back from zero (MEDIUM, not simulatable);
  - one targeted redis, a protected workload.
- **4 network faults escalated with the correct diagnosis.** No permitted action fixes a
  degraded network path, so handing it to a human is the correct result.
- **1 canary auto-mitigated.** Progressive delivery aborted the failing canary; AegisOps
  diagnosed it correctly and took no action.
- **1 transient pod kill** correctly produced no incident.
- **The sandbox blocked 2 wrong candidates** as Regressed: a rollback of order-service during a
  database-path fault, and restoring auth-service's broken "previous" configuration.

**How to read this.** 21/21 on one repetition says the delivered code handles each scenario
at least once. It does not say it handles them every time: the pass-rate interval runs down
to 85%. The scenario set also shaped the fixes in D17–D21. Run 1 (76%) is the fairer picture
of how a first encounter with these failure modes went. The honest summary of the three runs:

- every failure found was a real defect with a mechanism-level fix and a regression test;
- no run ever executed an unsafe or unacceptable action;
- the autonomy gates send about a third of incidents to a human.

Repetitions (`--repeat`) and new scenarios the rules have not seen are the next step.
