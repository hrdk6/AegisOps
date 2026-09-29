# Convenience wrapper. scripts/devctl.py is the source of truth and works without make (e.g. on Windows).
PY ?= python
VENV ?= aegis/.venv

.PHONY: up down deploy build status credentials dev test test-unit test-integration test-go test-frontend test-e2e lint crds benchmark

up:            ## create kind cluster, build + load images, deploy, wait
	$(PY) scripts/devctl.py up
down:
	$(PY) scripts/devctl.py down
deploy:
	$(PY) scripts/devctl.py deploy
build:         ## rebuild and load all images (or: make build C="aegis frontend")
	$(PY) scripts/devctl.py build $(C)
status:
	$(PY) scripts/devctl.py status
credentials:
	$(PY) scripts/devctl.py credentials

dev:           ## local Python environment for the CLI and tests
	uv venv $(VENV) --python 3.12
	uv pip install --python $(VENV) -e "aegis[dev,chaos]"
	cd frontend && npm ci

test: test-unit test-integration test-go test-frontend
test-unit:
	cd aegis && .venv/bin/python -m pytest -q tests/unit
test-integration: ## needs Docker (starts a throwaway PostgreSQL) or AEGIS_TEST_DATABASE_URL
	cd aegis && .venv/bin/python -m pytest -q tests/integration
test-e2e:      ## needs the running kind environment and AEGIS_TOKEN
	cd aegis && AEGIS_E2E=1 .venv/bin/python -m pytest -q tests/e2e
test-go:
	cd controlplane && go vet ./... && go test ./...
test-frontend:
	cd frontend && npx tsc --noEmit && npx next lint
lint:
	cd aegis && .venv/bin/python -m ruff check src tests
	cd controlplane && gofmt -l . && go vet ./...
crds:          ## regenerate deepcopy code and CRD manifests
	cd controlplane && controller-gen object:headerFile=hack/boilerplate.go.txt paths=./api/v1alpha1
	cd controlplane && controller-gen crd:allowDangerousTypes=true paths=./api/v1alpha1 output:crd:artifacts:config=../deploy/base/aegis/crds
benchmark:     ## live benchmark, all scenarios (REPEAT=3 for repetitions)
	cd aegis && .venv/bin/aegis benchmark run --repeat $(or $(REPEAT),1)
