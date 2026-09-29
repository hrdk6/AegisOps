"""Controlled fault injection for the demo/benchmark environment.

Faults are applied the way real incidents happen: a deployer ships a release
or config change, an operator mis-scales something, the network path degrades,
or traffic surges. Writes use the field manager "shop-deployer" so AegisOps
sees ordinary changes, not labelled faults.

Safety: the injector only touches the configured namespace, and only if that
namespace carries `aegisops.io/chaos-enabled=true`. It can only run scenarios
from the reviewed catalog; there is no free-form fault API.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from aegis.chaos.scenarios import Scenario

log = logging.getLogger("aegis.chaos")

FIELD_MANAGER = "shop-deployer"
IMAGE_REPO = "aegisops/shopflow"
RESOURCE_KEYS = {"cpuLimit": ("limits", "cpu"), "memoryLimit": ("limits", "memory"),
                 "cpuRequest": ("requests", "cpu"), "memoryRequest": ("requests", "memory")}


class ChaosError(Exception):
    pass


def load_golden(directory: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Declared (git) state of the managed namespace: Deployments and ConfigMaps by name."""
    deployments: dict[str, dict[str, Any]] = {}
    configmaps: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not doc:
                continue
            if doc.get("kind") == "Deployment":
                deployments[doc["metadata"]["name"]] = doc
            elif doc.get("kind") == "ConfigMap":
                configmaps[doc["metadata"]["name"]] = doc
    return deployments, configmaps


def signature(spec: dict[str, Any]) -> tuple[Any, ...]:
    """Operationally relevant fields of a Deployment spec (camelCase YAML or snake_case API dict)."""
    tmpl = spec["template"]
    c = tmpl["spec"]["containers"][0]
    env = sorted((e["name"], e.get("value"), bool(e.get("valueFrom") or e.get("value_from"))) for e in c.get("env") or [])
    res = {k: dict(v) for k, v in (c.get("resources") or {}).items() if k in ("limits", "requests") and v}
    labels = (tmpl.get("metadata") or {}).get("labels") or {}
    return (c.get("image"), tuple(env), tuple(sorted((k, tuple(sorted(v.items()))) for k, v in res.items())),
            spec.get("replicas", 1), labels.get("version"), bool(spec.get("paused")))


class Injector:
    def __init__(self, namespace: str, golden_dir: Path, network_url: str, loadgen_url: str) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.ns = namespace
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()
        self.custom = client.CustomObjectsApi()
        self.golden_deployments, self.golden_configmaps = load_golden(golden_dir)
        self.network = httpx.AsyncClient(base_url=network_url, timeout=5.0)
        self.loadgen = httpx.AsyncClient(base_url=loadgen_url, timeout=5.0)

    async def _k8s(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def guard(self) -> None:
        ns = await self._k8s(self.core.read_namespace, self.ns)
        if (ns.metadata.labels or {}).get("aegisops.io/chaos-enabled") != "true":
            raise ChaosError(f"namespace {self.ns} is not labelled aegisops.io/chaos-enabled=true; refusing to inject")

    async def _deployment(self, name: str) -> Any:
        return await self._k8s(self.apps.read_namespaced_deployment, name, self.ns)

    async def _patch_deployment(self, name: str, body: dict[str, Any], merge: bool = False) -> None:
        kwargs: dict[str, Any] = {"field_manager": FIELD_MANAGER}
        if merge:
            kwargs["_content_type"] = "application/merge-patch+json"
        await self._k8s(self.apps.patch_namespaced_deployment, name, self.ns, body, **kwargs)

    # --------------------------------------------------------------- inject --
    async def inject(self, sc: Scenario) -> dict[str, Any]:
        await self.guard()
        f = sc.fault
        now = datetime.now(UTC).isoformat()
        match f.type:
            case "release" | "env":
                env = dict(f.env)
                container: dict[str, Any] = {"name": f.target}
                labels: dict[str, str] = {}
                if f.type == "release":
                    if f.image_tag:
                        container["image"] = f"{IMAGE_REPO}:{f.image_tag}"
                    if f.version:
                        env["RELEASE_VERSION"] = f.version
                        labels["version"] = f.version
                container["env"] = [{"name": k, "value": v} for k, v in env.items()]
                body: dict[str, Any] = {"spec": {"template": {"spec": {"containers": [container]}}}}
                if labels:
                    body["spec"]["template"]["metadata"] = {"labels": labels}
                await self._patch_deployment(f.target, body)
                return {"applied": f"{f.type} change to {f.target}", "env": list(env), "at": now}
            case "resources":
                res: dict[str, dict[str, str]] = {}
                for key, value in f.resources.items():
                    section, kind = RESOURCE_KEYS[key]
                    res.setdefault(section, {})[kind] = value
                await self._patch_deployment(f.target, {"spec": {"template": {"spec": {"containers": [
                    {"name": f.target, "resources": res}]}}}})
                return {"applied": f"resources of {f.target} set to {res}", "at": now}
            case "scale":
                await self._patch_deployment(f.target, {"spec": {"replicas": f.replicas}})
                return {"applied": f"{f.target} scaled to {f.replicas}", "at": now}
            case "configmap":
                assert f.configmap
                await self._k8s(self.core.patch_namespaced_config_map, f.configmap, self.ns, {"data": f.data},
                                field_manager=FIELD_MANAGER)
                if f.restart:
                    await self._patch_deployment(f.target, {"spec": {"template": {"metadata": {"annotations": {
                        "kubectl.kubernetes.io/restartedAt": now}}}}})
                return {"applied": f"configmap {f.configmap} updated" + (f"; {f.target} restarted" if f.restart else ""),
                        "at": now}
            case "network":
                assert f.proxy
                toxic = {"name": f"degradation-{sc.id}"[:60], "stream": "downstream", "toxicity": 1.0, **f.toxic}
                resp = await self.network.post(f"/proxies/{f.proxy}/toxics", json=toxic)
                if resp.status_code >= 300:
                    raise ChaosError(f"network emulator rejected fault: {resp.status_code} {resp.text[:200]}")
                return {"applied": f"network toxic {toxic['type']} on path {f.proxy}", "at": now}
            case "traffic":
                resp = await self.loadgen.post("/control", json=f.traffic)
                resp.raise_for_status()
                return {"applied": f"traffic profile {f.traffic}", "at": now}
            case "pod_kill":
                pods = await self._k8s(self.core.list_namespaced_pod, self.ns, label_selector=f"app={f.target}")
                running = [p for p in pods.items if p.status.phase == "Running"]
                if not running:
                    raise ChaosError(f"no running pods for {f.target}")
                victim = running[0].metadata.name
                await self._k8s(self.core.delete_namespaced_pod, victim, self.ns, grace_period_seconds=0)
                return {"applied": f"pod {victim} deleted", "at": now}
            case "canary":
                c = f.canary
                body = {"apiVersion": "aegisops.io/v1alpha1", "kind": "CanaryRelease",
                        "metadata": {"name": f"{f.target}-{c['version'].replace('.', '-')}", "namespace": self.ns},
                        "spec": {"targetRef": f.target,
                                 "release": {"image": f"{IMAGE_REPO}:{c.get('image_tag', c['version'])}",
                                             "version": c["version"],
                                             "env": [{"name": "RELEASE_VERSION", "value": c["version"]}]
                                             + [{"name": k, "value": v} for k, v in c.get("env", {}).items()]},
                                 "steps": c.get("steps", [{"weight": 25, "pauseSeconds": 40}, {"weight": 50, "pauseSeconds": 40},
                                                          {"weight": 100, "pauseSeconds": 10}]),
                                 "analysis": c.get("analysis", {"maxErrorRate": 0.05, "maxP95Ms": 800, "minRequests": 20}),
                                 "totalReplicas": c.get("totalReplicas", 4)}}
                await self._k8s(self.custom.create_namespaced_custom_object, "aegisops.io", "v1alpha1", self.ns,
                                "canaryreleases", body, field_manager=FIELD_MANAGER)
                return {"applied": f"CanaryRelease {body['metadata']['name']} created", "at": now}
        raise ChaosError(f"unsupported fault type {f.type}")

    # ---------------------------------------------------------------- reset --
    async def drift(self) -> list[str]:
        out = []
        for name, golden in self.golden_deployments.items():
            try:
                live = (await self._k8s(self.apps.read_namespaced_deployment, name, self.ns)).to_dict()
            except ApiException:
                out.append(f"{name}: missing")
                continue
            if self._differs(golden, live):
                out.append(name)
        return out

    @staticmethod
    def _differs(golden: dict[str, Any], live: dict[str, Any]) -> bool:
        return signature(golden["spec"]) != signature(live["spec"])

    async def reset(self) -> dict[str, Any]:
        await self.guard()
        restored: list[str] = []
        for name, golden in self.golden_deployments.items():
            live = (await self._k8s(self.apps.read_namespaced_deployment, name, self.ns)).to_dict()
            if not self._differs(golden, live):
                continue
            g = copy.deepcopy(golden["spec"])
            body = {"spec": {"replicas": g.get("replicas", 1), "paused": False,
                             "template": {"metadata": {"labels": g["template"]["metadata"]["labels"]},
                                          "spec": {"containers": g["template"]["spec"]["containers"]}}}}
            await self._patch_deployment(name, body, merge=True)
            restored.append(name)
        for name, golden in self.golden_configmaps.items():
            live = await self._k8s(self.core.read_namespaced_config_map, name, self.ns)
            if (live.data or {}) != golden.get("data", {}):
                data: dict[str, Any] = dict(golden.get("data", {}))
                for k in (live.data or {}):
                    data.setdefault(k, None)
                await self._k8s(self.core.patch_namespaced_config_map, name, self.ns, {"data": data},
                                field_manager=FIELD_MANAGER)
                restored.append(f"configmap/{name}")
        removed_toxics = []
        try:
            proxies = (await self.network.get("/proxies")).json()
            for pname, proxy in proxies.items():
                for t in proxy.get("toxics", []):
                    await self.network.delete(f"/proxies/{pname}/toxics/{t['name']}")
                    removed_toxics.append(f"{pname}/{t['name']}")
        except httpx.HTTPError as exc:
            log.warning("network emulator reset failed", extra={"fields": {"error": str(exc)}})
        try:
            await self.loadgen.post("/control", json={"multiplier": 1.0, "checkout_multiplier": 1.0})
        except httpx.HTTPError as exc:
            log.warning("loadgen reset failed", extra={"fields": {"error": str(exc)}})
        canaries = await self._k8s(self.custom.list_namespaced_custom_object, "aegisops.io", "v1alpha1", self.ns,
                                   "canaryreleases")
        for c in canaries.get("items", []):
            await self._k8s(self.custom.delete_namespaced_custom_object, "aegisops.io", "v1alpha1", self.ns,
                            "canaryreleases", c["metadata"]["name"])
            restored.append(f"canary/{c['metadata']['name']}")
        pods = await self._k8s(self.core.list_namespaced_pod, self.ns, label_selector="aegisops.io/quarantined=true")
        for p in pods.items:
            await self._k8s(self.core.delete_namespaced_pod, p.metadata.name, self.ns)
            restored.append(f"pod/{p.metadata.name}")
        return {"restored": restored, "removed_toxics": removed_toxics, "at": datetime.now(UTC).isoformat()}

    async def status(self) -> dict[str, Any]:
        toxics: list[str] = []
        try:
            for pname, proxy in (await self.network.get("/proxies")).json().items():
                toxics += [f"{pname}/{t['name']} ({t['type']})" for t in proxy.get("toxics", [])]
        except httpx.HTTPError:
            toxics = ["network emulator unavailable"]
        try:
            traffic = (await self.loadgen.get("/status")).json()
        except httpx.HTTPError:
            traffic = {"error": "loadgen unavailable"}
        canaries = await self._k8s(self.custom.list_namespaced_custom_object, "aegisops.io", "v1alpha1", self.ns,
                                   "canaryreleases")
        return {"namespace": self.ns, "drift": await self.drift(), "toxics": toxics, "traffic": traffic,
                "canaries": [{"name": c["metadata"]["name"], "phase": c.get("status", {}).get("phase")}
                             for c in canaries.get("items", [])]}
