---
id: RB-005
title: Memory leak and OOMKilled containers
categories: [MEMORY_EXHAUSTION]
---
# Memory leak and OOMKilled containers

## Symptoms
- Working set grows steadily until the memory limit; containers are OOMKilled and restart.
- Errors or dropped work around each restart.

## Diagnosis
1. Check whether growth started after a deployment (leak introduced by a release).
2. Check whether the memory limit was recently lowered (see RB-003).

## Remediation
- Leak introduced by a release: roll back (`rollback_deployment`).
- Temporary mitigation: a rolling restart (`rollout_restart`) resets memory, but the leak recurs; watch for recurrence.
- Increasing the limit (`patch_resources`) only delays the next OOM.

## Verification
- Working set stable (no upward trend) over the recurrence watch window; no OOMKilled restarts.

## Prevention
- Bound in-process caches; alert on memory growth; soak-test releases.
