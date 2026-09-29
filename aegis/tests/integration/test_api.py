"""API security behaviour against a real database: authentication, RBAC, rate limiting,
request IDs, and HMAC-signed approvals forwarded to a (mocked) control plane."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import SecretStr

from aegis.api.main import create_app
from aegis.config import Settings
from aegis.db.models import ApprovalRecord, Incident
from aegis.db.session import Database

pytestmark = pytest.mark.integration

CP = "http://controlplane.test"
APPROVAL_KEY = "it-approval-key-" + "k" * 32
PASSWORDS = {"admin": "Admin-Passw0rd-IT", "viewer": "Viewer-Passw0rd-IT", "approver": "Approver-Passw0rd-IT"}


@pytest.fixture
async def client(pg_dsn: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=pg_dsn, jwt_secret=SecretStr("it-jwt-secret-" + "j" * 32), approval_key=SecretStr(APPROVAL_KEY),
        bootstrap_users=SecretStr(",".join(f"{u}:{p}:{u}" for u, p in PASSWORDS.items())),
        controlplane_url=CP, controlplane_token=SecretStr("cp-token"), controlplane_token_file=None,
        chaos_url="http://chaos.test", chaos_token=SecretStr("c" * 32), login_attempts_per_minute=5)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield c


async def login(c: httpx.AsyncClient, user: str) -> dict[str, str]:
    r = await c.post("/api/v1/auth/login", json={"username": user, "password": PASSWORDS[user]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_requires_authentication_and_sets_request_id(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/incidents")
    assert r.status_code == 401
    assert r.json()["error"]["code"] and r.headers.get("x-request-id")
    r = await client.get("/api/v1/incidents", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_login_failures_are_rate_limited(client: httpx.AsyncClient) -> None:
    # Each test gets a fresh app (and limiter); the limit is 5 attempts per minute per client address.
    codes = [(await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password-123"}))
             .status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]


async def test_viewer_cannot_act(client: httpx.AsyncClient) -> None:
    h = await login(client, "viewer")
    assert (await client.get("/api/v1/incidents", headers=h)).status_code == 200
    assert (await client.post("/api/v1/approvals/act-x/decision", json={"decision": "approved"}, headers=h)).status_code == 403
    assert (await client.put("/api/v1/policy/mode", json={"mode": "autonomous"}, headers=h)).status_code == 403
    assert (await client.post("/api/v1/demo/reset", headers=h)).status_code == 403
    assert (await client.get("/api/v1/audit", headers=h)).status_code == 403


async def _pending_approval(pg_dsn: str) -> tuple[str, str]:
    iid, aid = f"INC-IT-{secrets.token_hex(3).upper()}", f"act-it-{secrets.token_hex(4)}"
    db = Database(pg_dsn, pool_size=1)
    async with db.session() as s:
        s.add(Incident(id=iid, title="it", severity="SEV2", status="AWAITING_APPROVAL", detected_at=datetime.now(UTC),
                       affected_services=["payment-service"], symptoms=[], summary=""))
        await s.flush()
        s.add(ApprovalRecord(id=aid, incident_id=iid, status="pending", spec_hash="spec123", context={}))
    await db.dispose()
    return iid, aid


async def test_approval_is_signed_for_the_exact_spec(client: httpx.AsyncClient, pg_dsn: str) -> None:
    _, aid = await _pending_approval(pg_dsn)
    h = await login(client, "approver")
    sent: dict[str, object] = {}
    with respx.mock(base_url=CP, assert_all_called=False) as cp:
        cp.get(f"/v1/actions/{aid}").respond(json={"name": aid, "phase": "AwaitingApproval", "specHash": "0a1b2c3d"})

        def capture(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"name": aid, "phase": "Approved"})

        route = cp.post(f"/v1/actions/{aid}/approval").mock(side_effect=capture)
        r = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approved", "reason": "looks right"},
                              headers=h)
    assert r.status_code == 200, r.text
    assert route.called and route.calls.last.request.headers["authorization"] == "Bearer cp-token"
    reason_hash = hashlib.sha256(b"looks right").hexdigest()
    payload = "\n".join(["aegisops-approval-v1", aid, "0a1b2c3d", "approved", "approver", str(sent["issuedAt"]), reason_hash])
    assert sent["signature"] == hmac.new(APPROVAL_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert sent["specHash"] == "0a1b2c3d" and sent["approver"] == "approver"
    # A decided approval cannot be decided again.
    again = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "rejected"}, headers=h)
    assert again.status_code == 409


async def test_approval_refused_when_action_moved_on(client: httpx.AsyncClient, pg_dsn: str) -> None:
    _, aid = await _pending_approval(pg_dsn)
    h = await login(client, "approver")
    with respx.mock(base_url=CP) as cp:
        cp.get(f"/v1/actions/{aid}").respond(json={"name": aid, "phase": "Expired", "specHash": "0a1b2c3d"})
        r = await client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approved"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"


async def test_audit_records_logins(client: httpx.AsyncClient) -> None:
    h = await login(client, "admin")
    items = (await client.get("/api/v1/audit", params={"limit": 20}, headers=h)).json()["items"]
    assert any(i["action"] == "auth.login" and i["actor"] == "admin" and i["outcome"] == "success" for i in items)
    assert (await client.get("/api/v1/audit/verify", headers=h)).json()["valid"] is True
