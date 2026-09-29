---
id: RB-003
title: Resource limits set too low
categories: [RESOURCE_MISCONFIGURATION, MEMORY_EXHAUSTION, CPU_SATURATION]
---
# Resource limits set too low

## Symptoms
- After a resources change: containers OOMKilled (memory limit) or heavily CFS-throttled (CPU limit).
- Latency increases without a traffic increase; restarts increase.

## Diagnosis
1. Find the resources change in the change log and compare old and new limits.
2. Memory: working set near the limit, OOMKilled termination reason. CPU: throttled-period ratio high while usage sits at the limit.

## Remediation
- Restore the previous resource values (`patch_resources`) or roll back the deployment (`rollback_deployment`).
- Stay within policy maximums; large increases require approval.

## Verification
- No OOMKilled restarts; throttling ratio below 20%; p95 back under SLO.

## Prevention
- Right-size from observed usage (p95 working set plus headroom). Alert when usage exceeds 80% of limits.
