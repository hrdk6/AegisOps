"""Secret redaction applied to logs, evidence, prompts and model output.

Telemetry is untrusted and may contain credentials (connection strings, tokens,
keys). Everything that leaves a trust boundary (log sink, LLM prompt, API
response) passes through `redact`.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # URLs with embedded credentials: scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]+)@"), r"\1:[REDACTED]@"),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    # JWTs
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{5,}\b"), "[REDACTED_JWT]"),
    # Common provider API keys
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|nvapi-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,})\b"),
     "[REDACTED_KEY]"),
    # key=value / "key": "value" pairs with sensitive names
    (re.compile(r"(?i)\b([a-z0-9_]*(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)[a-z0-9_]*)"
                r"(\s*[=:]\s*\"?)([^\s\",}]{3,})"), r"\1\3[REDACTED]"),
    # PEM blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PEM]"),
]

_SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|authorization)")


def redact(text: str) -> str:
    """Redact secrets from free text."""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively redact dicts/lists; values under sensitive keys are masked."""
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k) and isinstance(v, str | int | float):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact_obj(v, _depth + 1) for v in value]
    return value


def sanitize_untrusted(text: str, max_len: int = 400) -> str:
    """Prepare untrusted telemetry text for inclusion in an LLM prompt:
    redact secrets, strip control characters, and bound its length."""
    text = redact(text)
    text = "".join(ch for ch in text if ch == "\n" or (ord(ch) >= 32 and ord(ch) != 127))
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text
