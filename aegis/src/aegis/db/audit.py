"""Hash-chained audit log.

Each record stores sha256(prev_hash || canonical(record)). Appends take a
transaction-scoped advisory lock so concurrent writers (API, engine) cannot
fork the chain. `verify_chain` recomputes every link and reports the first
break, so tampering is detectable even by someone who can bypass the
append-only trigger.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import AuditLog
from aegis.security.redaction import redact_obj

GENESIS = "0" * 64
_LOCK_KEY = 0x4AE615


def _canonical(ts: datetime, actor: str, action: str, resource: str, outcome: str, details: dict[str, Any],
               source: str) -> str:
    return json.dumps({"ts": ts.astimezone(UTC).isoformat(), "actor": actor, "action": action, "resource": resource,
                       "outcome": outcome, "details": details, "source": source}, sort_keys=True, default=str)


def compute_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


async def append(session: AsyncSession, *, actor: str, action: str, resource: str, outcome: str,
                 details: dict[str, Any] | None = None, source: str = "aegis", ts: datetime | None = None) -> AuditLog:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = (await session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))).scalar_one_or_none()
    prev = prev or GENESIS
    ts = ts or datetime.now(UTC)
    safe = redact_obj(details or {})
    digest = compute_hash(prev, _canonical(ts, actor, action, resource, outcome, safe, source))
    row = AuditLog(ts=ts, actor=actor[:120], action=action[:64], resource=resource[:200], outcome=outcome[:40],
                   details=safe, source=source, prev_hash=prev, hash=digest)
    session.add(row)
    await session.flush()
    return row


async def verify_chain(session: AsyncSession, limit: int | None = None) -> dict[str, Any]:
    q = select(AuditLog).order_by(AuditLog.id.asc())
    if limit:
        q = q.limit(limit)
    rows = (await session.execute(q)).scalars().all()
    prev = GENESIS
    for row in rows:
        expected = compute_hash(prev, _canonical(row.ts, row.actor, row.action, row.resource, row.outcome,
                                                 row.details, row.source))
        if row.prev_hash != prev or row.hash != expected:
            return {"valid": False, "checked": len(rows), "broken_at": row.id,
                    "reason": "prev_hash mismatch" if row.prev_hash != prev else "content hash mismatch"}
        prev = row.hash
    return {"valid": True, "checked": len(rows), "head": prev}
