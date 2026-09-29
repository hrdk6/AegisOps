# Incident scenarios

The benchmark and demo use 21 controlled fault scenarios against ShopFlow, defined in
`benchmarks/scenarios/*.yaml`. This page is generated from those files (the YAML is the source
of truth). Each scenario holds:

- **the fault**: what the chaos injector changes. It uses only the same Kubernetes operations a
  release engineer would (release a new image or env, edit a ConfigMap, change resources or
  replicas, kill a pod, degrade a network path, change traffic, start a canary), and only in
  the demo namespace;
- **ground truth**: root-cause categories and component; optimal, acceptable and prohibited
  remediations; acceptable outcomes. The engine never sees this. Only the scorer reads it.

Faults reproduce real *mechanisms* inside ShopFlow rather than returning canned errors. For
example: a rounding-mode flag makes a fraction of payments fail validation; a leak mode keeps
database connections checked out until the pool starves; a template cache grows without bound
until the container is OOM-killed; a slow fraud model burns CPU; a routing-table version
returns 503 for half the routes; a strict remote session check adds 600ms to every
authenticated request; the network proxy adds latency, resets connections, or black-holes
traffic between two services.

Outcomes: `resolved` (AegisOps fixed it and verification passed), `escalated` (correctly
handed to a human, which is the right answer for faults no permitted action can fix, such as
network degradation), `self_recovered`, `auto_mitigated` (for example progressive delivery
aborted the canary on its own), and `no_incident`.

| # | Scenario | Class | Root cause | Optimal remediation | Acceptable outcomes | Demo |
|---|---|---|---|---|---|---|
| 01 | `payment-bad-release` | bad deployment | BAD_DEPLOYMENT on payment-service | `rollback_deployment` → payment-service | resolved | ✓ |
| 02 | `order-slow-release` | bad deployment | BAD_DEPLOYMENT on order-service | `rollback_deployment` → order-service | resolved | |
| 03 | `inventory-invalid-currency` | config error | CONFIG_ERROR / BAD_DEPLOYMENT on inventory-service | `rollback_deployment` → inventory-service | resolved | |
| 04 | `auth-invalid-configmap` | config error | CONFIG_ERROR on auth-service | `update_config` → auth-service | resolved | ✓ |
| 05 | `redis-scaled-to-zero` | dependency outage | DEPENDENCY_FAILURE on redis | `scale_deployment` → redis | resolved | ✓ |
| 06 | `redis-network-partition` | network | NETWORK_DEGRADATION / DEPENDENCY_FAILURE on redis | none (escalate) | escalated | |
| 07 | `order-db-connection-leak` | pool exhaustion | DB_CONNECTION_EXHAUSTION / BAD_DEPLOYMENT on order-service | `rollback_deployment` → order-service | resolved | |
| 08 | `order-db-network-latency` | network | NETWORK_DEGRADATION on postgres | none (escalate) | escalated | |
| 09 | `payment-traffic-surge` | saturation | CPU_SATURATION on payment-service | `scale_deployment` → payment-service | resolved | |
| 10 | `payment-cpu-regression-release` | bad deployment | BAD_DEPLOYMENT / CPU_SATURATION on payment-service | `rollback_deployment` → payment-service | resolved | |
| 11 | `notification-memory-leak-release` | memory | MEMORY_EXHAUSTION / BAD_DEPLOYMENT on notification-service | `rollback_deployment` → notification-service | resolved | |
| 12 | `inventory-memory-limit-too-low` | resource misconfiguration | RESOURCE_MISCONFIGURATION on inventory-service | `patch_resources` → inventory-service | resolved | |
| 13 | `order-cpu-limit-too-low` | resource misconfiguration | RESOURCE_MISCONFIGURATION on order-service | `patch_resources` → order-service | resolved | |
| 14 | `payment-scaled-to-zero` | dependency outage | DEPENDENCY_FAILURE on payment-service | `scale_deployment` → payment-service | resolved | |
| 15 | `payment-pod-kill` | transient | none (transient) | none needed | no_incident, self_recovered | |
| 16 | `order-payment-network-latency` | network | NETWORK_DEGRADATION on payment-service | none (escalate) | escalated | |
| 17 | `order-payment-packet-loss` | network | NETWORK_DEGRADATION on payment-service | none (escalate) | escalated | |
| 18 | `payment-canary-regression` | canary | BAD_DEPLOYMENT on payment-service | `abort_canary` → the active canary | auto_mitigated, resolved, no_incident, self_recovered | ✓ |
| 19 | `gateway-routing-regression` | bad deployment | BAD_DEPLOYMENT on api-gateway | `rollback_deployment` → api-gateway | resolved | |
| 20 | `auth-slow-session-validation` | bad deployment | BAD_DEPLOYMENT on auth-service | `rollback_deployment` → auth-service | resolved | |
| 21 | `notification-scaled-to-zero` | dependency outage | DEPENDENCY_FAILURE on notification-service | `scale_deployment` → notification-service | resolved | |

## Details

### 01. Payment release with a ledger rounding regression (`payment-bad-release`)

payment-service 1.1.0 enables a new rounding mode that fails ~35% of charges.

- **Injected fault:** `release` on `payment-service`: `{"env": {"PAYMENT_ROUNDING_MODE": "bankers-v2"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: payment-service.error_ratio, order-service.error_ratio
- **Ground-truth root cause:** BAD_DEPLOYMENT on payment-service
- **Optimal remediation:** `rollback_deployment` → payment-service
- **Acceptable:** `rollback_deployment` → payment-service
- **Prohibited:** `rollback_deployment` → order-service, `rollback_deployment` → api-gateway, `scale_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 02. Order release adds a synchronous pre-authorization call (`order-slow-release`)

order-service 1.1.0 performs a 900ms synchronous fraud pre-check.

- **Injected fault:** `release` on `order-service`: `{"env": {"ORDER_SYNC_FRAUD_CHECK": "true"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: order-service.latency_p95
- **Ground-truth root cause:** BAD_DEPLOYMENT on order-service
- **Optimal remediation:** `rollback_deployment` → order-service
- **Acceptable:** `rollback_deployment` → order-service
- **Prohibited:** `rollback_deployment` → payment-service, `scale_deployment` → payment-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 03. Inventory release with an unsupported currency setting (`inventory-invalid-currency`)

inventory-service 1.1.0 sets CATALOG_CURRENCY=USDX; new pods fail validation.

- **Injected fault:** `release` on `inventory-service`: `{"env": {"CATALOG_CURRENCY": "USDX"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: inventory-service.crashloop
- **Ground-truth root cause:** CONFIG_ERROR / BAD_DEPLOYMENT on inventory-service
- **Optimal remediation:** `rollback_deployment` → inventory-service
- **Acceptable:** `rollback_deployment` → inventory-service
- **Prohibited:** `scale_deployment` → inventory-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 04. Broken auth configuration pushed to the ConfigMap (`auth-invalid-configmap`)

auth-config receives truncated JSON and auth-service is restarted to pick it up.

- **Injected fault:** `configmap` on `auth-service`: `{"configmap": "auth-config", "data": {"AUTH_CONFIG": "{\"token_ttl_seconds\": 3600, \"issuer\": "}, "restart": true}`
- **Detection:** required; expected symptoms: auth-service.crashloop, auth-service.unavailable_replicas
- **Ground-truth root cause:** CONFIG_ERROR on auth-service
- **Optimal remediation:** `update_config` → auth-service
- **Acceptable:** `update_config` → auth-service
- **Prohibited:** `scale_deployment` → auth-service, `rollback_deployment` → api-gateway, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 05. Redis accidentally scaled to zero (`redis-scaled-to-zero`)

An operator scales the shared Redis to 0 replicas; sessions and caches fail.

- **Injected fault:** `scale` on `redis`: `{"replicas": 0}`
- **Detection:** required; expected symptoms: redis.scaled_to_zero, api-gateway.error_ratio
- **Ground-truth root cause:** DEPENDENCY_FAILURE on redis
- **Optimal remediation:** `scale_deployment` → redis
- **Acceptable:** `scale_deployment` → redis
- **Prohibited:** `rollback_deployment` → order-service, `rollback_deployment` → auth-service, `rollout_restart` → auth-service, `rollout_restart` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 06. Network path to Redis black-holes traffic (`redis-network-partition`)

Traffic from order-service and auth-service to Redis is silently dropped (Redis itself is healthy).

- **Injected fault:** `network` on `redis`: `{"proxy": "redis", "toxic": {"attributes": {"timeout": 0}, "type": "timeout"}}`
- **Detection:** required; expected symptoms: api-gateway.error_ratio
- **Ground-truth root cause:** NETWORK_DEGRADATION / DEPENDENCY_FAILURE on redis
- **Optimal remediation:** none (escalate)
- **Acceptable:** none (escalate)
- **Prohibited:** `rollback_deployment` → *, `rollout_restart` → order-service, `rollout_restart` → auth-service, `scale_deployment` → redis, `rollout_restart` → redis, any action → postgres, any action → shop-network
- **Acceptable outcomes:** escalated

### 07. Order release leaks database connections (`order-db-connection-leak`)

order-service 1.1.0 reconciles inline and leaks a connection on ~8% of orders.

- **Injected fault:** `release` on `order-service`: `{"env": {"ORDER_RECONCILE_MODE": "inline"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: order-service.db_pool_saturation, order-service.error_ratio
- **Ground-truth root cause:** DB_CONNECTION_EXHAUSTION / BAD_DEPLOYMENT on order-service
- **Optimal remediation:** `rollback_deployment` → order-service
- **Acceptable:** `rollback_deployment` → order-service, `rollout_restart` → order-service
- **Prohibited:** `rollback_deployment` → payment-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 08. Network latency between order-service and PostgreSQL (`order-db-network-latency`)

The network path from order-service to PostgreSQL adds ~350ms per round trip; the database is healthy.

- **Injected fault:** `network` on `postgres`: `{"proxy": "postgres", "toxic": {"attributes": {"jitter": 50, "latency": 350}, "type": "latency"}}`
- **Detection:** required; expected symptoms: order-service.latency_p95
- **Ground-truth root cause:** NETWORK_DEGRADATION on postgres
- **Optimal remediation:** none (escalate)
- **Acceptable:** none (escalate)
- **Prohibited:** `rollback_deployment` → order-service, `rollout_restart` → order-service, `scale_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** escalated

### 09. Checkout traffic surge saturates payment CPU (`payment-traffic-surge`)

Checkout traffic rises 8x (marketing campaign); payment-service fraud scoring becomes CPU-bound.

- **Injected fault:** `traffic` on `payment-service`: `{"traffic": {"checkout_multiplier": 8.0, "multiplier": 1.0}}`
- **Detection:** required; expected symptoms: payment-service.cpu_saturation, payment-service.latency_p95
- **Ground-truth root cause:** CPU_SATURATION on payment-service
- **Optimal remediation:** `scale_deployment` → payment-service
- **Acceptable:** `scale_deployment` → payment-service
- **Prohibited:** `rollback_deployment` → payment-service, `rollout_restart` → payment-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 10. Payment release ships an expensive fraud model (`payment-cpu-regression-release`)

payment-service 1.1.0 switches to fraud model gbdt-v2 (6x CPU per request).

- **Injected fault:** `release` on `payment-service`: `{"env": {"FRAUD_MODEL": "gbdt-v2"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: payment-service.latency_p95, payment-service.cpu_throttling
- **Ground-truth root cause:** BAD_DEPLOYMENT / CPU_SATURATION on payment-service
- **Optimal remediation:** `rollback_deployment` → payment-service
- **Acceptable:** `rollback_deployment` → payment-service, `scale_deployment` → payment-service
- **Prohibited:** `rollback_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 11. Notification release with an unbounded template cache (`notification-memory-leak-release`)

notification-service 1.1.0 caches rendered templates without eviction.

- **Injected fault:** `release` on `notification-service`: `{"env": {"TEMPLATE_CACHE": "unbounded"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: notification-service.oom_killed, notification-service.memory_pressure
- **Ground-truth root cause:** MEMORY_EXHAUSTION / BAD_DEPLOYMENT on notification-service
- **Optimal remediation:** `rollback_deployment` → notification-service
- **Acceptable:** `rollback_deployment` → notification-service, `rollout_restart` → notification-service
- **Prohibited:** `rollback_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 12. Inventory memory limit lowered below its working set (`inventory-memory-limit-too-low`)

A resources change sets inventory-service memory limit to 48Mi; new pods are OOMKilled at startup.

- **Injected fault:** `resources` on `inventory-service`: `{"resources": {"memoryLimit": "48Mi", "memoryRequest": "32Mi"}}`
- **Detection:** required; expected symptoms: inventory-service.crashloop, inventory-service.oom_killed
- **Ground-truth root cause:** RESOURCE_MISCONFIGURATION on inventory-service
- **Optimal remediation:** `patch_resources` → inventory-service
- **Acceptable:** `patch_resources` → inventory-service, `rollback_deployment` → inventory-service
- **Prohibited:** `scale_deployment` → inventory-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 13. Order CPU limit lowered to 40m (`order-cpu-limit-too-low`)

A resources change caps order-service at 40 millicores; requests queue behind CFS throttling.

- **Injected fault:** `resources` on `order-service`: `{"resources": {"cpuLimit": "40m", "cpuRequest": "20m"}}`
- **Detection:** required; expected symptoms: order-service.latency_p95, order-service.cpu_throttling
- **Ground-truth root cause:** RESOURCE_MISCONFIGURATION on order-service
- **Optimal remediation:** `patch_resources` → order-service
- **Acceptable:** `patch_resources` → order-service, `rollback_deployment` → order-service
- **Prohibited:** `rollback_deployment` → payment-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 14. Payment service scaled to zero (`payment-scaled-to-zero`)

payment-service is scaled to 0 replicas; every checkout fails.

- **Injected fault:** `scale` on `payment-service`: `{"replicas": 0}`
- **Detection:** required; expected symptoms: payment-service.scaled_to_zero, order-service.error_ratio
- **Ground-truth root cause:** DEPENDENCY_FAILURE on payment-service
- **Optimal remediation:** `scale_deployment` → payment-service
- **Acceptable:** `scale_deployment` → payment-service
- **Prohibited:** `rollback_deployment` → order-service, `rollout_restart` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 15. Single payment pod terminated (`payment-pod-kill`)

One payment-service pod is deleted; the ReplicaSet replaces it within seconds.

- **Injected fault:** `pod_kill` on `payment-service`
- **Detection:** optional
- **Ground-truth root cause:** none (transient)
- **Optimal remediation:** none needed
- **Acceptable:** none needed
- **Prohibited:** any action (a transient fault must not trigger remediation)
- **Acceptable outcomes:** no_incident, self_recovered

### 16. Network latency between order-service and payment-service (`order-payment-network-latency`)

The network path order -> payment adds ~800ms; payment itself is fast.

- **Injected fault:** `network` on `payment-service`: `{"proxy": "payment", "toxic": {"attributes": {"jitter": 100, "latency": 800}, "type": "latency"}}`
- **Detection:** required; expected symptoms: order-service.latency_p95
- **Ground-truth root cause:** NETWORK_DEGRADATION on payment-service
- **Optimal remediation:** none (escalate)
- **Acceptable:** none (escalate)
- **Prohibited:** `rollback_deployment` → payment-service, `scale_deployment` → payment-service, `rollout_restart` → payment-service, `rollback_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** escalated

### 17. Connection resets between order-service and payment-service (`order-payment-packet-loss`)

30% of connections on the order -> payment path are reset by the network.

- **Injected fault:** `network` on `payment-service`: `{"proxy": "payment", "toxic": {"attributes": {"timeout": 0}, "toxicity": 0.3, "type": "reset_peer"}}`
- **Detection:** required; expected symptoms: order-service.error_ratio
- **Ground-truth root cause:** NETWORK_DEGRADATION on payment-service
- **Optimal remediation:** none (escalate)
- **Acceptable:** none (escalate)
- **Prohibited:** `rollback_deployment` → payment-service, `rollout_restart` → payment-service, `scale_deployment` → payment-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** escalated

### 18. Canary release of payment 1.2.0 fails its SLO gate (`payment-canary-regression`)

payment-service 1.2.0 is shipped progressively; the canary returns errors and must be aborted.

- **Injected fault:** `canary` on `payment-service`: `{"canary": {"env": {"PAYMENT_ROUNDING_MODE": "bankers-v2"}, "image_tag": "1.2.0", "totalReplicas": 4, "version": "1.2.0"}}`
- **Detection:** optional; expected symptoms: payment-service.error_ratio
- **Ground-truth root cause:** BAD_DEPLOYMENT on payment-service
- **Optimal remediation:** `abort_canary` → the active canary
- **Acceptable:** `abort_canary` → the active canary
- **Prohibited:** `rollback_deployment` → payment-service, `rollback_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** auto_mitigated, resolved, no_incident, self_recovered

### 19. Gateway release with a broken routing table (`gateway-routing-regression`)

api-gateway 1.1.0 loads routing table v2, which fails half of POST /api/orders with 503.

- **Injected fault:** `release` on `api-gateway`: `{"env": {"ROUTING_TABLE_VERSION": "2"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: api-gateway.error_ratio
- **Ground-truth root cause:** BAD_DEPLOYMENT on api-gateway
- **Optimal remediation:** `rollback_deployment` → api-gateway
- **Acceptable:** `rollback_deployment` → api-gateway
- **Prohibited:** `rollback_deployment` → order-service, `scale_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 20. Auth release validates sessions remotely (`auth-slow-session-validation`)

auth-service 1.1.0 validates every session with a slow remote call (+600ms).

- **Injected fault:** `release` on `auth-service`: `{"env": {"SESSION_VALIDATION": "strict-remote"}, "image_tag": "1.1.0", "version": "1.1.0"}`
- **Detection:** required; expected symptoms: auth-service.latency_p95
- **Ground-truth root cause:** BAD_DEPLOYMENT on auth-service
- **Optimal remediation:** `rollback_deployment` → auth-service
- **Acceptable:** `rollback_deployment` → auth-service
- **Prohibited:** `rollback_deployment` → api-gateway, `scale_deployment` → api-gateway, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved

### 21. Notification service scaled to zero (`notification-scaled-to-zero`)

notification-service is scaled to 0; order confirmations fail (orders still succeed).

- **Injected fault:** `scale` on `notification-service`: `{"replicas": 0}`
- **Detection:** required; expected symptoms: notification-service.scaled_to_zero
- **Ground-truth root cause:** DEPENDENCY_FAILURE on notification-service
- **Optimal remediation:** `scale_deployment` → notification-service
- **Acceptable:** `scale_deployment` → notification-service
- **Prohibited:** `rollback_deployment` → order-service, any action → postgres, any action → shop-network
- **Acceptable outcomes:** resolved
