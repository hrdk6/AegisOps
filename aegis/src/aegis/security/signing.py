"""HMAC signing of human approvals (mirror of controlplane/internal/approval).

Only the API process holds the approval key. The engine never does, so a
compromised reasoning layer cannot forge a human approval.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def _mac(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def sign_decision(key: bytes, action_name: str, spec_hash: str, decision: str, approver: str, reason: str,
                  issued_at: int | None = None) -> dict[str, object]:
    issued = issued_at or int(time.time())
    reason_hash = hashlib.sha256(reason.encode()).hexdigest()
    payload = "\n".join(["aegisops-approval-v1", action_name, spec_hash, decision, approver, str(issued), reason_hash])
    return {"specHash": spec_hash, "decision": decision, "approver": approver, "reason": reason, "issuedAt": issued,
            "signature": _mac(key, payload)}


def sign_mode_change(key: bytes, mode: str, approver: str, issued_at: int | None = None) -> dict[str, object]:
    issued = issued_at or int(time.time())
    payload = "\n".join(["aegisops-mode-v1", mode, approver, str(issued)])
    return {"mode": mode, "approver": approver, "issuedAt": issued, "signature": _mac(key, payload)}
