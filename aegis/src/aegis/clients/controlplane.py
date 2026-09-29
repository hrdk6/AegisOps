"""Client for the Go control-plane API — the AI layer's only path to Kubernetes.

The engine authenticates with an audience-bound projected ServiceAccount token
(reader+proposer roles). It can read state, dry-run policy, and *request*
actions and simulations; the controller decides and executes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from aegis.clients.http import UpstreamUnavailable


class ControlPlaneError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"control plane {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ControlPlane:
    def __init__(self, base_url: str, token: Callable[[], str]) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(35.0, connect=3.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = await self.http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("controlplane", str(exc)) from exc
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise ControlPlaneError(resp.status_code, err.get("code", "error"), err.get("message", resp.text[:200]))
        return resp.json()

    # ---- read-only state -------------------------------------------------
    async def health(self) -> dict[str, Any]:
        try:
            resp = await self.http.get("/v1/health")
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamUnavailable("controlplane", str(exc)) from exc

    async def workloads(self, namespace: str) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/workloads", params={"namespace": namespace}))["items"]

    async def workload(self, namespace: str, name: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/workloads/{namespace}/{name}")

    async def topology(self, namespace: str) -> dict[str, Any]:
        return await self._call("GET", "/v1/topology", params={"namespace": namespace})

    async def changes(self, since: datetime, namespace: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        params = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if namespace:
            params["namespace"] = namespace
        if name:
            params["name"] = name
        return (await self._call("GET", "/v1/changes", params=params))["items"]

    async def signals(self, since: datetime, namespace: str | None = None) -> list[dict[str, Any]]:
        params = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if namespace:
            params["namespace"] = namespace
        return (await self._call("GET", "/v1/signals", params=params))["items"]

    async def policy(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/policy")

    async def canaries(self, namespace: str) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/canaries", params={"namespace": namespace}))["items"]

    async def audit(self, after: int, boot: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"after": after}
        if boot:
            params["boot"] = boot
        return await self._call("GET", "/v1/audit", params=params)

    # ---- policy & actions ------------------------------------------------
    async def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/policy/evaluate", json=request)

    async def submit_action(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/actions", json=request)

    async def action(self, name: str, wait_for_version: str | None = None, timeout: int = 25) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if wait_for_version:
            params = {"waitForVersion": wait_for_version, "timeoutSeconds": timeout}
        return await self._call("GET", f"/v1/actions/{name}", params=params)

    async def actions(self, incident: str | None = None) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/actions", params={"incident": incident} if incident else None))["items"]

    async def revert(self, name: str, justification: str) -> dict[str, Any]:
        return await self._call("POST", f"/v1/actions/{name}/revert", json={"justification": justification})

    async def approve(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/v1/actions/{name}/approval", json=body)

    async def set_mode(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PUT", "/v1/policy/mode", json=body)

    # ---- simulations -----------------------------------------------------
    async def submit_simulation(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/simulations", json=request)

    async def simulation(self, name: str, wait_for_version: str | None = None, timeout: int = 25) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if wait_for_version:
            params = {"waitForVersion": wait_for_version, "timeoutSeconds": timeout}
        return await self._call("GET", f"/v1/simulations/{name}", params=params)

    async def simulations(self, incident: str | None = None) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/simulations", params={"incident": incident} if incident else None))["items"]
