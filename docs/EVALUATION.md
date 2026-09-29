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
aegis/.venv/bin/aegis benchmark run              # all 21 scenarios, 1 repetition (about 2.5 hours)
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
