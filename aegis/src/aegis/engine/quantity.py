"""Kubernetes resource quantity parsing (subset used for CPU/memory)."""

from __future__ import annotations

import re

_SUFFIX = {
    "": 1.0, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "Ki": 1024.0, "Mi": 1024.0**2, "Gi": 1024.0**3, "Ti": 1024.0**4,
}
_RE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]{0,2})\s*$")


def parse_quantity(value: str | None) -> float | None:
    if not value:
        return None
    m = _RE.match(str(value))
    if not m or m.group(2) not in _SUFFIX:
        return None
    return float(m.group(1)) * _SUFFIX[m.group(2)]


def format_memory(num_bytes: float) -> str:
    return f"{max(1, round(num_bytes / 1024**2))}Mi"


def format_cpu(cores: float) -> str:
    return f"{max(1, round(cores * 1000))}m"
