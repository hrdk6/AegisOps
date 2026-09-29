---
id: RB-004
title: CPU saturation under load
categories: [CPU_SATURATION]
---
# CPU saturation under load

## Symptoms
- CPU usage at or near the limit, throttling, rising p95 latency and queueing.
- Request rate well above baseline (traffic surge) with no recent deployment.

## Diagnosis
1. Compare the request rate with the pre-incident baseline.
2. Check that no release or resource change explains the extra CPU (otherwise see RB-001 or RB-003).
3. Traces: most request time is spent in the saturated service's own spans.

## Remediation
- Scale out the saturated deployment (`scale_deployment`) proportionally to utilization (target about 60% CPU), within policy limits.
- If saturation follows a release that made requests more expensive, roll back instead (RB-001).

## Verification
- CPU utilization below 70% of limit, p95 back under SLO, no new errors.

## Prevention
- Configure a HorizontalPodAutoscaler for CPU-bound services; load-test capacity per replica.
