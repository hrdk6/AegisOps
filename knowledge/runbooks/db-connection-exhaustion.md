---
id: RB-006
title: Database connection pool exhaustion
categories: [DB_CONNECTION_EXHAUSTION]
---
# Database connection pool exhaustion

## Symptoms
- Pool in-use equals pool size, requests waiting for connections, acquire timeouts.
- Latency spikes and 5xx on the affected service while the database server itself is healthy.

## Diagnosis
1. Confirm the database is healthy and other services' database calls are fast (the problem is local to one client).
2. Look for a code path that holds connections (long transactions, missing release on an error path), often introduced by a release.

## Remediation
- If a deployment introduced the leak: roll back (`rollback_deployment`).
- Mitigation: a rolling restart of the leaking service (`rollout_restart`) releases leaked connections; expect recurrence while the leak persists.
- Do not restart or scale the database: it is protected and is not the cause.

## Verification
- Pool utilization below 70%, zero acquire timeouts, error ratio under SLO, no recurrence in the watch window.

## Prevention
- Enforce statement and idle-in-transaction timeouts; test connection release on error paths.
