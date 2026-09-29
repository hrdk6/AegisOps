"""Release-dependent behaviours.

ShopFlow's failure modes are tied to realistic configuration: release flags,
data-path options and resource settings that a deployment pipeline might
change. They are *not* labelled as faults; an operator (or AegisOps) has to
infer from telemetry that a given change caused a problem.
"""

from __future__ import annotations

import hashlib
import random
import time

from shopflow.config import flag

_retained: list[bytearray] = []


def cpu_work(milliseconds: float) -> None:
    """Burn roughly `milliseconds` of CPU time (fraud-scoring stand-in)."""
    deadline = time.process_time() + milliseconds / 1000.0
    digest = b"shopflow"
    while time.process_time() < deadline:
        for _ in range(200):
            digest = hashlib.sha256(digest).digest()


def retain_memory(kilobytes: int) -> None:
    """Retain memory that is never released (unbounded cache stand-in)."""
    # Filled with non-zero bytes so pages are actually resident (RSS grows).
    _retained.append(bytearray(b"t" * (kilobytes * 1024)))


def fraud_model_cost_ms() -> float:
    """CPU cost of the configured fraud model."""
    return {"gbdt-v1": 15.0, "gbdt-v2": 90.0}.get(flag("FRAUD_MODEL", "gbdt-v1"), 15.0)


def rounding_mismatch() -> bool:
    """The bankers-v2 rounding mode miscomputes ledger totals for some amounts."""
    return flag("PAYMENT_ROUNDING_MODE", "half-up") == "bankers-v2" and random.random() < 0.35


def reconcile_leaks_connection() -> bool:
    """Inline reconciliation holds a connection open on its error path."""
    return flag("ORDER_RECONCILE_MODE", "async") == "inline" and random.random() < 0.08


def sync_fraud_check_delay() -> float:
    return 0.9 if flag("ORDER_SYNC_FRAUD_CHECK", "false") == "true" else 0.0


def session_validation_delay() -> float:
    return 0.6 if flag("SESSION_VALIDATION", "local") == "strict-remote" else 0.0


def template_cache_leak_kb() -> int:
    return 600 if flag("TEMPLATE_CACHE", "lru") == "unbounded" else 0


def routing_table_miss() -> bool:
    return flag("ROUTING_TABLE_VERSION", "1") == "2" and random.random() < 0.5
