"""Service configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Logical dependency graph of ShopFlow (HTTP dependencies only). Data stores
# are declared separately. The same graph is published to Kubernetes through
# the aegisops.io/dependencies annotation on each Deployment.
HTTP_DEPENDENCIES: dict[str, list[str]] = {
    "storefront": ["api-gateway"],
    "api-gateway": ["auth-service", "order-service", "inventory-service"],
    "auth-service": [],
    "order-service": ["inventory-service", "payment-service", "notification-service"],
    "payment-service": [],
    "notification-service": [],
    "inventory-service": [],
}

DATA_DEPENDENCIES: dict[str, list[str]] = {
    "auth-service": ["postgres", "redis"],
    "order-service": ["postgres", "redis"],
    "payment-service": ["postgres"],
    "inventory-service": ["postgres", "redis"],
}


def _env_name(dep: str) -> str:
    return dep.upper().replace("-", "_") + "_URL"


def _flag(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    service: str
    version: str
    port: int
    pod: str
    sandbox: bool
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    db_pool_size: int
    db_acquire_timeout: float
    redis_url: str
    upstreams: dict[str, str] = field(default_factory=dict)
    upstream_timeout: float = 2.5
    otlp_endpoint: str = ""

    @property
    def uses_postgres(self) -> bool:
        return "postgres" in DATA_DEPENDENCIES.get(self.service, [])

    @property
    def uses_redis(self) -> bool:
        return "redis" in DATA_DEPENDENCIES.get(self.service, [])

    @classmethod
    def from_env(cls, service: str) -> Settings:
        if service not in HTTP_DEPENDENCIES:
            raise ValueError(f"unknown service {service!r}")
        sandbox = _flag("AEGIS_SANDBOX") == "1"
        upstreams = {}
        for dep in HTTP_DEPENDENCIES[service]:
            url = _flag(_env_name(dep), f"http://{dep}:8080")
            upstreams[dep] = url.rstrip("/")
        return cls(
            service=service,
            version=_flag("RELEASE_VERSION", "1.0.0"),
            port=int(_flag("PORT", "8080")),
            pod=_flag("POD_NAME", os.environ.get("HOSTNAME", "local")),
            sandbox=sandbox,
            # In the AegisOps sandbox, data stores are pod-local sidecars.
            db_host="127.0.0.1" if sandbox else _flag("DB_HOST", "postgres"),
            db_port=5432 if sandbox else int(_flag("DB_PORT", "5432")),
            db_user=_flag("DB_USER", "shop"),
            db_password="sandbox" if sandbox else _flag("DB_PASSWORD", "shop"),
            db_name=_flag("DB_NAME", "shop"),
            db_pool_size=int(_flag("DB_POOL_SIZE", "8")),
            db_acquire_timeout=float(_flag("DB_ACQUIRE_TIMEOUT", "2.0")),
            redis_url="redis://127.0.0.1:6379/0" if sandbox else _flag("REDIS_URL", "redis://redis:6379/0"),
            upstreams=upstreams,
            upstream_timeout=float(_flag("UPSTREAM_TIMEOUT", "2.5")),
            otlp_endpoint=_flag("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )


def flag(name: str, default: str = "") -> str:
    """Read a release/feature flag."""
    return _flag(name, default)
