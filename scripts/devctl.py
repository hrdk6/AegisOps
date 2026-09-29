#!/usr/bin/env python3
"""AegisOps local environment manager (stdlib only; works on Linux, macOS and Windows).

  python scripts/devctl.py up          # create cluster, build, load, deploy, wait (idempotent)
  python scripts/devctl.py build [component...]   # controlplane shopflow aegis frontend
  python scripts/devctl.py load [component...] [--third-party]   # load images into kind
  python scripts/devctl.py deploy      # (re)apply manifests
  python scripts/devctl.py restart <deployment> [namespace]
  python scripts/devctl.py status
  python scripts/devctl.py credentials
  python scripts/devctl.py down        # delete the kind cluster
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLUSTER = os.environ.get("AEGIS_CLUSTER", "aegisops")
CONTEXT = f"kind-{CLUSTER}"
STATE_DIR = ROOT / ".aegis-dev"
SECRETS_FILE = STATE_DIR / "secrets.json"
SHOPFLOW_TAGS = ["1.0.0", "1.1.0", "1.2.0"]

IMAGES = {
    "controlplane": ("aegisops/controlplane:dev", ["docker", "build", "-t", "aegisops/controlplane:dev", "controlplane"]),
    "shopflow": ("aegisops/shopflow:dev", ["docker", "build", "-t", "aegisops/shopflow:dev", "shopflow"]),
    "aegis": ("aegisops/aegis:dev", ["docker", "build", "-t", "aegisops/aegis:dev", "-f", "aegis/Dockerfile", "."]),
    "frontend": ("aegisops/frontend:dev", ["docker", "build", "-t", "aegisops/frontend:dev", "frontend"]),
}
THIRD_PARTY = ["postgres:16-alpine", "redis:7.4-alpine", "prom/prometheus:v3.4.1",
               "grafana/grafana:12.0.2", "grafana/loki:3.5.1", "jaegertracing/jaeger:2.7.0",
               "otel/opentelemetry-collector-contrib:0.128.0"]
PORTS = {"AegisOps UI": "http://localhost:3000", "AegisOps API (OpenAPI docs)": "http://localhost:8000/docs",
         "Grafana": "http://localhost:3001", "Jaeger": "http://localhost:16686", "Prometheus": "http://localhost:9090",
         "ShopFlow storefront": "http://localhost:8080"}


def which(tool: str) -> str:
    found = shutil.which(tool)
    if found:
        return found
    for extra in (Path.home() / "go" / "bin", Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "gotools"):
        for name in (tool, tool + ".exe"):
            if (extra / name).exists():
                return str(extra / name)
    sys.exit(f"required tool not found on PATH: {tool}")


def sh(*cmd: str, check: bool = True, capture: bool = False, input_: str | None = None) -> subprocess.CompletedProcess[str]:
    exe = which(cmd[0])
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run([exe, *cmd[1:]], cwd=ROOT, check=check, text=True, input=input_,
                          capture_output=capture)


def kubectl(*args: str, check: bool = True, capture: bool = False, input_: str | None = None) -> subprocess.CompletedProcess[str]:
    return sh("kubectl", "--context", CONTEXT, *args, check=check, capture=capture, input_=input_)


def cluster_exists() -> bool:
    out = subprocess.run([which("kind"), "get", "clusters"], capture_output=True, text=True, check=False).stdout
    return CLUSTER in out.split()


def create_cluster() -> None:
    if cluster_exists():
        print(f"kind cluster '{CLUSTER}' already exists")
        return
    sh("kind", "create", "cluster", "--name", CLUSTER, "--config", "deploy/kind/cluster.yaml", "--wait", "120s")


def build(components: list[str]) -> None:
    for c in components or list(IMAGES):
        if c == "frontend" and not (ROOT / "frontend" / "Dockerfile").exists():
            continue
        sh(*IMAGES[c][1])
        if c == "shopflow":
            for tag in SHOPFLOW_TAGS:
                sh("docker", "tag", "aegisops/shopflow:dev", f"aegisops/shopflow:{tag}")


def load(components: list[str], third_party: bool = False) -> None:
    images: list[str] = []
    for c in components or list(IMAGES):
        if c == "frontend" and not (ROOT / "frontend" / "Dockerfile").exists():
            continue
        images.append(IMAGES[c][0])
        if c == "shopflow":
            images += [f"aegisops/shopflow:{t}" for t in SHOPFLOW_TAGS]
    sh("kind", "load", "docker-image", "--name", CLUSTER, *images)
    if third_party:
        # Optional pre-seeding. With Docker Desktop's containerd image store, `docker save` of
        # multi-platform pulls can omit blobs and the import fails; the node then pulls normally.
        for img in THIRD_PARTY:
            if sh("kind", "load", "docker-image", "--name", CLUSTER, img, check=False, capture=True).returncode != 0:
                print(f"  (could not pre-load {img}; the node will pull it)")


def ensure_secrets() -> dict[str, str]:
    STATE_DIR.mkdir(exist_ok=True)
    if SECRETS_FILE.exists():
        values = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    else:
        values = {k: secrets.token_urlsafe(32) for k in (
            "shop_db", "aegis_db", "approval_key", "jwt_secret", "chaos_token", "grafana_admin",
            "pw_admin", "pw_approver", "pw_operator", "pw_viewer")}
        SECRETS_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except OSError:
            pass
    users = ",".join(f"{u}:{values['pw_' + u]}:{u}" for u in ("admin", "approver", "operator", "viewer"))
    specs = [
        ("shop", "shop-db", {"password": values["shop_db"]}),
        ("aegis-system", "aegis-db", {"password": values["aegis_db"]}),
        ("aegis-system", "aegis-approval-key", {"key": values["approval_key"]}),
        ("aegis-system", "aegis-api", {"jwt-secret": values["jwt_secret"]}),
        ("aegis-system", "aegis-chaos", {"token": values["chaos_token"]}),
        ("aegis-system", "aegis-users", {"users": users}),
        ("observability", "grafana-admin", {"password": values["grafana_admin"]}),
    ]
    for ns, name, data in specs:
        args = ["create", "secret", "generic", name, "-n", ns, "--dry-run=client", "-o", "yaml"]
        args += [f"--from-literal={k}={v}" for k, v in data.items()]
        manifest = kubectl(*args, capture=True).stdout
        kubectl("apply", "-f", "-", input_=manifest, capture=True)
    llm_env = {k: os.environ[k] for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
                                          "OLLAMA_BASE_URL", "AEGIS_LLM_ROUTES") if os.environ.get(k)}
    if llm_env:
        args = ["create", "secret", "generic", "aegis-llm", "-n", "aegis-system", "--dry-run=client", "-o", "yaml"]
        args += [f"--from-literal={k}={v}" for k, v in llm_env.items()]
        applied = kubectl("apply", "-f", "-", input_=kubectl(*args, capture=True).stdout, capture=True).stdout
        print(f"model provider configuration loaded from environment: {sorted(llm_env)}")
        # Pods read the Secret only at start, so pick up changed keys or routes immediately.
        if "unchanged" not in applied and kubectl("get", "deploy/aegis-engine", "-n", "aegis-system", check=False,
                                                  capture=True).returncode == 0:
            kubectl("rollout", "restart", "deploy/aegis-engine", "-n", "aegis-system")
    return values


def deploy() -> None:
    kubectl("apply", "-k", "deploy/base/aegis/crds")
    kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=60s")
    kubectl("apply", "-f", "deploy/base/namespaces.yaml")
    ensure_secrets()
    kubectl("apply", "-k", "deploy/overlays/dev")


def wait_ready(timeout: int = 600) -> None:
    deadline = time.time() + timeout
    for ns in ("observability", "shop", "traffic", "aegis-system"):
        remaining = max(30, int(deadline - time.time()))
        kubectl("rollout", "status", "deployment", "-n", ns, "--timeout", f"{remaining}s", check=False)
    kubectl("rollout", "status", "daemonset/otel-collector", "-n", "observability", "--timeout", "120s", check=False)


def restart(name: str, ns: str = "aegis-system") -> None:
    kubectl("rollout", "restart", f"deployment/{name}", "-n", ns)
    kubectl("rollout", "status", f"deployment/{name}", "-n", ns, "--timeout", "180s")


def credentials() -> None:
    values = json.loads(SECRETS_FILE.read_text(encoding="utf-8")) if SECRETS_FILE.exists() else {}
    print("\nLocal endpoints:")
    for k, v in PORTS.items():
        print(f"  {k:<30} {v}")
    if values:
        print(f"\nDemo accounts (generated; stored in {SECRETS_FILE.relative_to(ROOT)}, git-ignored):")
        for u in ("admin", "approver", "operator", "viewer"):
            print(f"  {u:<9} {values['pw_' + u]}")
        print(f"  grafana admin password: {values['grafana_admin']}")


def status() -> None:
    kubectl("get", "pods", "-A", "-o", "wide", check=False)
    kubectl("get", "remediationactions,remediationsimulations", "-n", "aegis-system", check=False)
    kubectl("get", "aegispolicies,canaryreleases", "-A", check=False)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from the git-ignored .env; variables already set in the shell win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)


def main() -> None:
    load_dotenv()
    args = sys.argv[1:] or ["help"]
    cmd, rest = args[0], args[1:]
    if cmd == "up":
        create_cluster()
        build(rest)
        load(rest, third_party=True)
        deploy()
        wait_ready()
        credentials()
    elif cmd == "build":
        build(rest)
        load(rest)
    elif cmd == "load":
        load([c for c in rest if not c.startswith("--")], third_party="--third-party" in rest)
    elif cmd == "deploy":
        deploy()
        wait_ready()
    elif cmd == "restart":
        restart(rest[0], rest[1] if len(rest) > 1 else "aegis-system")
    elif cmd == "status":
        status()
    elif cmd == "credentials":
        credentials()
    elif cmd == "down":
        sh("kind", "delete", "cluster", "--name", CLUSTER)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
