---
id: RB-002
title: Invalid application configuration
categories: [CONFIG_ERROR, POD_CRASHLOOP]
---
# Invalid application configuration

## Symptoms
- New pods fail to start (CrashLoopBackOff) or fail readiness after a ConfigMap or environment change.
- Startup logs contain "configuration invalid", parse or validation errors.
- The rolling update stalls; capacity drops if old pods were already terminated.

## Diagnosis
1. Check the change log for a ConfigMap data change or an env var change consumed by the failing workload.
2. Read the critical startup log line: it names the invalid key.
3. Rolling back the Deployment does NOT fix a bad ConfigMap: the old pod template reads the same ConfigMap.

## Remediation
- ConfigMap changes: restore the previous recorded revision (`update_config` with `configRevision: previous`), which also restarts the consumers.
- Env or flag changes in the pod template: roll back the deployment (`rollback_deployment`).
- Do not scale up a crash-looping workload; it only adds failing pods.

## Verification
- All replicas ready, zero restarts in the verification window, callers' error ratios back under SLO.

## Prevention
- Validate configuration in CI (schema checks) and at admission time.
- Use immutable, versioned ConfigMaps referenced by name from the pod template.
