"""The closed loop against the live environment, plus safety properties that must hold there."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest

pytestmark = pytest.mark.e2e


async def test_every_component_is_healthy(api: httpx.AsyncClient) -> None:
    health = (await api.get("/api/v1/system/health")).json()
    bad = [c for c in health["components"] if c["status"] not in ("ok", "idle")]
    assert not bad, bad


@pytest.mark.parametrize("action", ["exec_command", "delete_workload", "modify_rbac", "delete_volume", "schema_migration"])
async def test_prohibited_actions_are_denied_by_the_live_policy(api: httpx.AsyncClient, action: str) -> None:
    r = await api.post("/api/v1/policy/check", json={"action_type": action, "target": "payment-service"})
    assert r.status_code == 200, r.text
    d = r.json()["decision"]
    assert d["allowed"] is False and d["riskLevel"] == "CRITICAL"


async def test_protected_database_cannot_be_automated(api: httpx.AsyncClient) -> None:
    d = (await api.post("/api/v1/policy/check", json={"action_type": "scale_deployment", "target": "postgres",
                                                      "parameters": {"replicas": 0}})).json()["decision"]
    assert d["allowed"] is False
    assert any(not c["passed"] for c in d["checks"] if c["name"] in ("protected-workload", "limits"))


async def test_nonexistent_target_is_denied(api: httpx.AsyncClient) -> None:
    # The API pins the managed namespace; namespace scoping itself is exercised in the Go tests.
    d = (await api.post("/api/v1/policy/check", json={"action_type": "restart_pod", "target": "no-such-pod",
                                                      "target_kind": "Pod"})).json()["decision"]
    assert d["allowed"] is False


async def test_audit_chain_is_intact(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/audit/verify")).json()["valid"] is True


async def _wait_stable(api: httpx.AsyncClient, timeout: float = 420) -> None:
    await api.post("/api/v1/demo/reset")
    deadline, streak = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        active = (await api.get("/api/v1/incidents", params={"status": "active"})).json()["items"]
        services = (await api.get("/api/v1/services")).json()["items"]
        healthy = all(s["status"] == "healthy" for s in services if s.get("replicas"))
        streak = streak + 1 if healthy and not active else 0
        if streak >= 12:
            return
        await asyncio.sleep(5)
    pytest.fail("environment did not stabilise")


async def test_bad_release_is_rolled_back_autonomously(api: httpx.AsyncClient) -> None:
    """Flagship loop: detect -> diagnose -> simulate -> policy (auto) -> rollback -> verify -> postmortem."""
    await _wait_stable(api)
    injected = datetime.now(UTC)
    assert (await api.post("/api/v1/demo/scenarios/payment-bad-release/inject")).status_code == 200
    incident = None
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        items = (await api.get("/api/v1/incidents", params={"limit": 5})).json()["items"]
        mine = [i for i in items if datetime.fromisoformat(i["detected_at"]) >= injected]
        if mine:
            incident = (await api.get(f"/api/v1/incidents/{mine[-1]['id']}")).json()
            if incident["status"] in ("RESOLVED", "ESCALATED") and incident["has_postmortem"]:
                break
        await asyncio.sleep(5)
    assert incident is not None, "no incident opened"
    assert incident["status"] == "RESOLVED" and incident["outcome"] == "resolved_autonomously", incident["outcome"]
    sel = incident["diagnosis"]["selected"]
    assert sel["category"] == "BAD_DEPLOYMENT" and sel["component"] == "payment-service"
    executed = [a for a in incident["actions"] if a["phase"] == "Succeeded"]
    assert [a["action_type"] for a in executed] == ["rollback_deployment"]
    assert incident["simulations"] and incident["simulations"][0]["verdict"] == "Improved"
    assert incident["verifications"][-1]["outcome"] == "RESOLVED"
    pm = (await api.get(f"/api/v1/incidents/{incident['id']}/postmortem")).json()
    claims = [c for s in pm["content"]["sections"] for c in s["claims"]]
    assert {c["type"] for c in claims} >= {"FACT", "HYPOTHESIS"}
    await _wait_stable(api)
