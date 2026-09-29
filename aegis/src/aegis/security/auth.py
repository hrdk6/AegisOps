"""Password hashing, JWT sessions and the role/permission matrix."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from enum import StrEnum
from typing import Any

import jwt

from aegis.domain.enums import Role


class Permission(StrEnum):
    READ = "read"
    INVESTIGATE = "investigate"
    RESOLVE = "resolve"
    APPROVE = "approve"
    DEMO = "demo"
    BENCHMARK = "benchmark"
    POLICY_ADMIN = "policy_admin"
    AUDIT = "audit"


ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.VIEWER: {Permission.READ},
    Role.OPERATOR: {Permission.READ, Permission.INVESTIGATE, Permission.RESOLVE, Permission.DEMO, Permission.BENCHMARK},
    Role.APPROVER: {Permission.READ, Permission.INVESTIGATE, Permission.RESOLVE, Permission.DEMO, Permission.BENCHMARK,
                    Permission.APPROVE, Permission.AUDIT},
    Role.ADMIN: set(Permission),
}

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


# A precomputed hash used to equalize timing when the user does not exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def issue_token(secret: str, username: str, role: str, ttl_minutes: int) -> tuple[str, int]:
    now = int(time.time())
    exp = now + ttl_minutes * 60
    claims = {"sub": username, "role": role, "iat": now, "exp": exp, "jti": secrets.token_hex(8), "iss": "aegisops"}
    return jwt.encode(claims, secret, algorithm="HS256"), exp


def decode_token(secret: str, token: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=["HS256"], issuer="aegisops",
                      options={"require": ["exp", "iat", "sub", "role"]})


def permissions_for(role: str) -> set[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return set()


def parse_bootstrap_users(spec: str) -> list[tuple[str, str, str]]:
    """'user:password:role,...' -> [(user, password, role)]; weak passwords are rejected."""
    out = []
    for entry in (e.strip() for e in spec.split(",") if e.strip()):
        user, password, role = entry.split(":", 2)
        Role(role)  # validates
        if len(password) < 12:
            raise ValueError(f"bootstrap password for {user} is too short (min 12 chars)")
        out.append((user, password, role))
    return out
