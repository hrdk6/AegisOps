---
id: RB-001
title: Regression introduced by a deployment
categories: [BAD_DEPLOYMENT]
---
# Regression introduced by a deployment

## Symptoms
- Error rate or latency of a service rises within minutes after a rollout (new image, version or feature flag).
- Errors concentrate in pods of the newest ReplicaSet; older pods (if still running) are healthy.
- Failing traces originate in the changed service; upstream services fail only as victims.

## Diagnosis
1. Correlate anomaly onset with the change log: a release/env change on the same workload shortly before onset.
2. Compare error ratio by `pod_template_hash` (new vs old ReplicaSet).
3. Confirm dependencies of the changed service are healthy (otherwise the release may be a coincidence).

## Remediation
- Roll back the deployment to the previous revision (`rollback_deployment`). This restores the last known-good pod template.
- If a rollout is still in progress, pause it first (`pause_rollout`) to stop further blast radius.
- Do not scale or restart unrelated services; they are victims.

## Verification
- Error ratio and p95 of the service and its upstream callers return below SLO within about a minute of rollout completion.
- New ReplicaSet pods are replaced by the previous revision.

## Prevention
- Ship risky changes through progressive delivery (CanaryRelease) with SLO gates.
- Add pre-production tests for the failing code path; keep new feature flags off by default.
