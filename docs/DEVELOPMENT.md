# Development

## Prerequisites

| Tool | Version used | Needed for |
|---|---|---|
| Docker (Desktop or Engine) | 29.x | building images, kind nodes, integration tests |
| kind | v0.30.0 (Kubernetes v1.34.0 node) | local cluster |
| kubectl | ≥ 1.31 | deploy and inspect |
| Python | 3.12 (via [uv](https://docs.astral.sh/uv/)) | CLI, engine and API tests, `scripts/devctl.py` (stdlib only, any Python ≥ 3.10) |
| Go | 1.26 | controller tests and CRD generation (images build Go inside Docker) |
| controller-gen | v0.19.0 | regenerating CRDs and deepcopy code |
| Node.js | ≥ 22 (npm ≥ 10) | frontend type-check, lint, build |

Resources: the full environment (ShopFlow, observability stack, AegisOps, sandbox
simulations) runs on one kind node. Give Docker at least 4 CPUs and 8 GB of memory.

## One-command environment

```bash
python scripts/devctl.py up
```

This command, which is idempotent:

1. creates the kind cluster `aegisops` from `deploy/kind/cluster.yaml`, with NodePorts mapped
   to localhost;
2. builds images: `aegisops/controlplane`, `aegisops/aegis` (API, engine, chaos, migrations),
   `aegisops/shopflow` (tagged `1.0.0`, `1.1.0`, `1.2.0` for release scenarios) and
   `aegisops/frontend`;
3. loads them into the node, and pre-loads third-party images where possible;
4. applies CRDs and namespaces, generates secrets into `.aegis-dev/secrets.json` (git-ignored)
   and Kubernetes Secrets, and applies `deploy/overlays/dev`;
5. waits for rollouts and prints endpoints and demo credentials.

| Endpoint | URL |
|---|---|
| AegisOps UI | http://localhost:3000 |
| AegisOps API (OpenAPI) | http://localhost:8000/docs |
| Grafana | http://localhost:3001 |
| Jaeger | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| ShopFlow storefront | http://localhost:8080 |

Other commands: `devctl.py build [controlplane|aegis|shopflow|frontend]` (rebuild and load),
`deploy`, `restart <deployment> [namespace]`, `status`, `credentials`, `down`. After
rebuilding an image, restart its Deployment, for example
`kubectl -n aegis-system rollout restart deploy/aegis-engine`.

To enable model-assisted diagnosis, export one or more of `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`GROQ_API_KEY`, `NVIDIA_API_KEY`, `OLLAMA_BASE_URL` (and optionally `AEGIS_LLM_ROUTES`) before
running `deploy` or `up`. Without them, everything runs on the deterministic path.

## Local Python environment (CLI and tests)

```bash
uv venv aegis/.venv --python 3.12
uv pip install --python aegis/.venv -e "aegis[dev,chaos]"
export AEGIS_API_URL=http://localhost:8000
aegis/.venv/bin/aegis login --username admin     # password: python scripts/devctl.py credentials
aegis/.venv/bin/aegis status
```

On Windows use `aegis\.venv\Scripts\aegis.exe`.

## Tests

| Suite | Command | What it covers |
|---|---|---|
| Python unit (79) | `cd aegis && .venv/bin/python -m pytest -q tests/unit` | Diagnosis rules (including regressions from live runs and the benchmark), calibration, model router (retry, fallback, budget, timeouts), grounding and prompt-injection resistance, redaction, detection persistence, trace blame, planning and utility, fail-closed policy, verification outcomes, evaluation statistics and scoring, detector degradation (a failing query is unknown, not zero traffic), signing vector |
| Python integration (10) | `cd aegis && .venv/bin/python -m pytest -q tests/integration` | Real PostgreSQL (throwaway container, or `AEGIS_TEST_DATABASE_URL`) with the production migrations: append-only enforcement, audit-chain tamper detection, redaction before hashing, escalation hold, orphaned approvals, API authentication, RBAC denials, login rate limit, signed approval forwarding (mocked controller), invalid-state refusal |
| Python e2e | `cd aegis && AEGIS_E2E=1 AEGIS_TOKEN=... .venv/bin/python -m pytest -q tests/e2e` | Against the running kind environment (10): component health, live-policy denial of all five prohibited action classes, protected-database denial, nonexistent-target denial, audit-chain integrity, and the full autonomous loop on the flagship scenario |
| Go (58) | `cd controlplane && go vet ./... && go test ./...` | Policy engine (every check, including stale-plan preconditions), registry validation, approval signatures (freshness, tampering, cross-language vector), sandbox builder (secret and identity stripping, verdicts, probe parsing), controllers against a fake client (autonomous scale and revert, approval expiry, execution-timeout auto-revert, incident budget, simulation hash persistence), executor stale-plan guard, rollout-status and crash-loop checks that ignore pre-existing faults, idempotent `update_config` re-apply, HTTP API (unauthenticated rejection, engine cannot approve, approver cannot propose, command smuggling, namespace scope, signature required) |
| ShopFlow (9) | `cd shopflow && ../aegis/.venv/bin/python -m pytest -q` | The network fault proxy (pass-through, latency on established connections and clean removal, resets, black-holing and recovery, control API validation) and configuration-driven failure modes |
| Frontend | `cd frontend && npx tsc --noEmit && npx next build` | Types, lint, production build |
| Benchmark | `aegis benchmark run` | 21 live scenarios scored against ground truth; see [EVALUATION.md](EVALUATION.md) |

## Code layout and conventions

- **Python** (`aegis/src/aegis`): async throughout, typed with pydantic models at every
  boundary, `ruff` with line length 120. Modules: `api/` (FastAPI routes, deps, SSE),
  `engine/` (detector, context, diagnosis, planner, orchestrator, verification, postmortem,
  topology, signatures), `ai/` (router, providers, prompts, contracts), `clients/`
  (Prometheus, Loki, Jaeger, control plane), `db/` (models, repo, audit, migrations),
  `security/` (auth, signing, redaction), `chaos/`, `evaluation/`, `cli/`, `knowledge/`.
- **Go** (`controlplane/`): `api/v1alpha1` (CRD types with kubebuilder markers),
  `internal/policy` (pure evaluation), `internal/registry`, `internal/actions` (executor,
  templates), `internal/sandbox`, `internal/controllers`, `internal/httpapi`,
  `internal/approval`, `internal/state` (read models), `internal/changelog`,
  `internal/history`, `internal/promclient`, `internal/audit`, `internal/telemetry`.
- **Frontend** (`frontend/`): App Router, client components with SWR, and event-driven
  revalidation from the SSE stream. The design tokens are CSS variables in
  `app/globals.css`, with light and dark themes.

After changing CRD types:

```bash
cd controlplane
controller-gen object:headerFile=hack/boilerplate.go.txt paths=./api/v1alpha1
controller-gen crd:allowDangerousTypes=true paths=./api/v1alpha1 output:crd:artifacts:config=../deploy/base/aegis/crds
kubectl apply -f ../deploy/base/aegis/crds/ --server-side --force-conflicts
```

Database schema changes go in a new Alembic revision under
`aegis/src/aegis/db/migrations/versions/`. Migrations run as an init container of `aegis-api`.

## Extending

- **A new remediation action.** Add the type and parameters to `api/v1alpha1`, register it
  in `internal/registry` (base risk, target kinds, allowed and required parameters,
  reversible, simulatable, disruptive), implement it in `internal/actions/executor.go` (apply,
  snapshot, revert, progress), add policy prerequisites, add it to
  `aegis/src/aegis/domain/enums.py`, and add a playbook entry in `engine/planner.py`. Write
  policy tests first.
- **A new diagnosis rule.** Add a `rule_*` function in `engine/diagnosis/rules.py` that reasons
  about *mechanisms* (never service names), cites supporting and contradicting evidence ids,
  and is registered in `RULES`. Add a unit test with a bundle from `tests/conftest.py`
  helpers, including a "near miss" that must *not* fire.
- **A new scenario.** Add `benchmarks/scenarios/NN-name.yaml` with the fault, ground truth
  (root cause categories and component, optimal, acceptable and prohibited actions,
  acceptable outcomes) and timeouts. Fault types are implemented in
  `aegis/src/aegis/chaos/injector.py` and are restricted to the demo namespace.
- **A new model provider.** Implement `ai/providers/base.py` (or reuse the OpenAI-compatible
  provider with a base URL) and register it in `ai/router.py`.
