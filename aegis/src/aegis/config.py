"""Validated configuration loaded from environment variables (prefix AEGIS_)."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"

    # PostgreSQL state store.
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "aegis"
    db_password: SecretStr = SecretStr("aegis")
    db_name: str = "aegis"
    database_url: str | None = None  # full override (used by tests)

    # Control plane (Go controller) API.
    controlplane_url: str = "http://aegis-controlplane.aegis-system.svc:8082"
    controlplane_token_file: Path | None = Path("/var/run/secrets/aegis/token")
    controlplane_token: SecretStr | None = None  # development only
    managed_namespace: str = "shop"

    # Telemetry backends.
    prometheus_url: str = "http://prometheus.observability.svc:9090"
    loki_url: str = "http://loki.observability.svc:3100"
    jaeger_url: str = "http://jaeger.observability.svc:16686"
    grafana_public_url: str = "http://localhost:3001"
    jaeger_public_url: str = "http://localhost:16686"

    # API security.
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 480
    approval_key: SecretStr = SecretStr("")
    bootstrap_users: SecretStr = SecretStr("")  # "user:password:role,..."
    cors_origins: str = "http://localhost:3000"  # comma-separated
    rate_limit_per_minute: int = 240
    login_attempts_per_minute: int = 10

    # Demo fault injection service.
    chaos_url: str = "http://aegis-chaos.aegis-system.svc:8090"
    chaos_token: SecretStr = SecretStr("")
    demo_mode: bool = True

    # Detection and workflow tuning.
    detect_interval_seconds: float = 5.0
    detection_persistence: int = 2
    incident_merge_window_seconds: int = 600
    # An escalated incident keeps absorbing anomalies on its services while symptoms persist; a symptom-free
    # gap longer than this ends the hold, so a later, separate fault opens a new incident.
    escalation_hold_seconds: int = 45
    max_remediation_rounds: int = 3
    verification_timeout_seconds: int = 90
    verification_min_settle_seconds: int = 15
    recurrence_watch_seconds: int = 60
    self_recovery_seconds: int = 45
    simulation_timeout_seconds: int = 180
    approval_wait_seconds: int = 900
    slo_file: Path = Path("/app/config/slos.yaml")
    runbook_dir: Path = Path("/app/knowledge/runbooks")
    scenario_dir: Path = Path("/app/benchmarks/scenarios")

    # AI layer.
    llm_routes: str = ""  # JSON: {"diagnosis": ["openai:gpt-4.1", "heuristic"], ...}
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    llm_max_calls_per_incident: int = 8
    llm_prices: str = ""  # JSON: {"model": [input_per_1m, output_per_1m]}

    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def dsn(self) -> str:
        if self.database_url:
            return self.database_url
        pw = self.db_password.get_secret_value()
        return f"postgresql+asyncpg://{self.db_user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def raw_dsn(self) -> str:
        """DSN for plain asyncpg (LISTEN/NOTIFY)."""
        return self.dsn.replace("postgresql+asyncpg://", "postgresql://")

    def controlplane_bearer(self) -> str:
        if self.controlplane_token is not None:
            return self.controlplane_token.get_secret_value()
        if self.controlplane_token_file and self.controlplane_token_file.exists():
            return self.controlplane_token_file.read_text(encoding="utf-8").strip()
        return ""

    def fingerprint(self) -> str:
        """Hash of behaviour-relevant configuration (for experiment metadata)."""
        relevant = {
            k: v for k, v in self.model_dump(mode="json").items()
            if k.startswith(("detect", "detection", "incident", "max_", "verification", "recurrence", "self_", "llm_routes"))
        }
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]

    def validate_for(self, component: str) -> None:
        """Fail fast when a component starts without required secrets."""
        missing = []
        if component == "api":
            if len(self.jwt_secret.get_secret_value()) < 32:
                missing.append("AEGIS_JWT_SECRET (>=32 chars)")
            if len(self.approval_key.get_secret_value()) < 32:
                missing.append("AEGIS_APPROVAL_KEY (>=32 chars)")
        if component == "chaos" and len(self.chaos_token.get_secret_value()) < 24:
            missing.append("AEGIS_CHAOS_TOKEN (>=24 chars)")
        if missing:
            raise RuntimeError("missing or weak configuration: " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()
