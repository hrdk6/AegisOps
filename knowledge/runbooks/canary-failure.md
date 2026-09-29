---
id: RB-009
title: Failed canary release
categories: [BAD_DEPLOYMENT]
---
# Failed canary release

## Symptoms
- During a progressive rollout, canary pods show a higher error rate or latency than stable pods.
- Canary analysis verdict is Fail; the controller aborts and returns traffic to stable.

## Diagnosis
1. Compare canary and stable error ratio and p95 at the same traffic weight.
2. Confirm the difference is not explained by an unrelated dependency incident.

## Remediation
- If the canary is still progressing: abort it (`abort_canary`). Stable pods keep serving.
- If the controller has already aborted it: no further action is needed; verify recovery.

## Verification
- Canary deployment removed, stable at full capacity, error ratio back under SLO.

## Prevention
- Keep canary steps small with tight SLO gates; require passing analysis before promotion.
