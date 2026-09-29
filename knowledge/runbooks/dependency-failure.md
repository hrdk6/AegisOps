---
id: RB-007
title: Unavailable dependency (cache, database, internal service)
categories: [DEPENDENCY_FAILURE]
---
# Unavailable dependency

## Symptoms
- Several services that share a dependency fail at the same time with connection errors to it.
- The dependency has zero ready replicas (scaled to zero, crashed, or evicted).

## Diagnosis
1. Use the dependency graph: find the common downstream dependency of the failing services.
2. Check its replica count and the change log (was it scaled down?).

## Remediation
- Restore the dependency's previous replica count (`scale_deployment`). Stateful dependencies require human approval under policy.
- Do not roll back the callers; they are victims.

## Verification
- Dependency ready; callers' client-side error ratios and 5xx return to baseline.

## Prevention
- Protect critical dependencies with PodDisruptionBudgets and admission policies that forbid scaling to zero.
