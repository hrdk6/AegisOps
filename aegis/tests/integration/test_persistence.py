"""Database invariants: tamper-evident audit chain, append-only enforcement, escalation hold, action updates."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aegis.db import audit, repo
from aegis.db.models import ActionRecord, AuditLog, Incident
from aegis.db.session import Database
from aegis.domain.enums import IncidentStatus

pytestmark = pytest.mark.integration


async def test_audit_chain_detects_tampering(db: Database) -> None:
    tag = secrets.token_hex(4)
    async with db.session() as s:
        for i in range(3):
            await audit.append(s, actor="it", action="test.append", resource=f"it/{tag}/{i}", outcome="ok",
                               details={"i": i, "token": "Bearer abcdefghijklmnopqrstuvwxyz"})
    async with db.session() as s:
        assert (await audit.verify_chain(s))["valid"] is True
        row = (await s.execute(select(AuditLog).where(AuditLog.resource == f"it/{tag}/1"))).scalar_one()
        assert "abcdefghijklmnop" not in str(row.details), "secrets must be redacted before hashing"
        target_id, original = row.id, row.outcome

    # Append-only: the database itself rejects edits and deletes.
    with pytest.raises(DBAPIError, match="append-only"):
        async with db.session() as s:
            await s.execute(text("UPDATE audit_log SET outcome = 'forged' WHERE id = :id"), {"id": target_id})
    with pytest.raises(DBAPIError, match="append-only"):
        async with db.session() as s:
            await s.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": target_id})

    # Someone with superuser access bypasses the trigger: the hash chain still exposes the edit.
    async with db.session() as s:
        await s.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update"))
        await s.execute(text("UPDATE audit_log SET outcome = 'forged' WHERE id = :id"), {"id": target_id})
    try:
        async with db.session() as s:
            res = await audit.verify_chain(s)
        assert res["valid"] is False and res["broken_at"] == target_id and res["reason"] == "content hash mismatch"
    finally:
        async with db.session() as s:
            await s.execute(text("UPDATE audit_log SET outcome = :o WHERE id = :id"), {"o": original, "id": target_id})
            await s.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update"))
    async with db.session() as s:
        assert (await audit.verify_chain(s))["valid"] is True


async def _incident(db: Database, status: IncidentStatus, services: list[str], updated: datetime,
                    resolved: datetime | None = None) -> str:
    iid = f"INC-IT-{secrets.token_hex(3).upper()}"
    async with db.session() as s:
        s.add(Incident(id=iid, title="it", severity="SEV2", status=status.value, detected_at=updated - timedelta(minutes=2),
                       onset_at=updated - timedelta(minutes=3), affected_services=services, symptoms=[], summary="",
                       resolved_at=resolved))
    async with db.session() as s:  # updated_at has an onupdate default; pin it explicitly
        await s.execute(text("UPDATE incidents SET updated_at = :u WHERE id = :id"), {"u": updated, "id": iid})
    return iid


async def test_escalated_incident_holds_only_while_symptomatic(db: Database) -> None:
    now = datetime.now(UTC)
    svc = f"svc-{secrets.token_hex(3)}"
    held = await _incident(db, IncidentStatus.ESCALATED, [svc, "api-gateway"], updated=now - timedelta(seconds=10))
    await _incident(db, IncidentStatus.ESCALATED, [f"other-{svc}"], updated=now)  # disjoint services
    since = now - timedelta(seconds=45)
    async with db.session() as s:
        got = await repo.escalated_holding(s, [svc], since)
        assert got is not None and got.id == held
        assert await repo.escalated_holding(s, [f"unrelated-{svc}"], since) is None
        # A symptom-free gap longer than the hold ends it.
        assert await repo.escalated_holding(s, [svc], now) is None
    stale_svc = f"stale-{svc}"
    await _incident(db, IncidentStatus.ESCALATED, [stale_svc], updated=now - timedelta(minutes=5))
    closed_svc = f"closed-{svc}"
    await _incident(db, IncidentStatus.ESCALATED, [closed_svc], updated=now, resolved=now)
    async with db.session() as s:
        assert await repo.escalated_holding(s, [stale_svc], since) is None
        assert await repo.escalated_holding(s, [closed_svc], since) is None, "human-closed incidents release the hold"


async def test_update_action_never_inserts(db: Database) -> None:
    missing = f"act-it-{secrets.token_hex(4)}"
    async with db.session() as s:
        await repo.update_action(s, missing, outcome="resolved")
    async with db.session() as s:
        assert await s.get(ActionRecord, missing) is None


async def test_orphaned_approvals_are_closed(db: Database) -> None:
    from aegis.db.models import ApprovalRecord

    now = datetime.now(UTC)
    closed = await _incident(db, IncidentStatus.ESCALATED, [f"svc-{secrets.token_hex(3)}"], updated=now)
    open_ = await _incident(db, IncidentStatus.AWAITING_APPROVAL, [f"svc-{secrets.token_hex(3)}"], updated=now)
    async with db.session() as s:
        for iid in (closed, open_):
            s.add(ApprovalRecord(id=f"act-{iid.lower()}", incident_id=iid, status="pending", spec_hash="h", context={}))
    async with db.session() as s:
        swept = await repo.close_pending_approvals(s, "expired")
    assert f"act-{closed.lower()}" in swept and f"act-{open_.lower()}" not in swept, "only closed incidents are swept"
    async with db.session() as s:
        assert (await s.get(ApprovalRecord, f"act-{open_.lower()}")).status == "pending"  # type: ignore[union-attr]
        await repo.close_pending_approvals(s, "superseded", open_)
    async with db.session() as s:
        assert (await s.get(ApprovalRecord, f"act-{open_.lower()}")).status == "superseded"  # type: ignore[union-attr]
