---
id: RB-008
title: Network path degradation between services
categories: [NETWORK_DEGRADATION]
---
# Network path degradation between services

## Symptoms
- Client-side latency or connection resets on one edge (caller to dependency) while the dependency's own server-side metrics are healthy.
- Trace gap: client spans are much longer than the matching server spans.
- Other callers of the same dependency are unaffected.

## Diagnosis
1. Compare caller-side p95 with the dependency's server-side p95 and with other callers.
2. Check for recent changes on both sides; network issues usually have none.

## Remediation
- There is no safe automated remediation inside the application layer: restarting or rolling back either service does not fix the network path.
- Escalate to the platform or network owner with the edge, timing and evidence.
- Consider temporary load shedding or timeout tuning under human supervision.

## Verification
- Client-side latency and error ratio on the edge return to baseline.

## Prevention
- Alert on client/server latency divergence per edge; use timeouts and retry budgets.
