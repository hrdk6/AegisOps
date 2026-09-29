"""SLO definitions (config/slos.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SLO:
    service: str
    max_error_ratio: float = 0.02
    p95_ms: float = 500.0
    min_rps: float = 0.2
    cpu_util: float = 0.9
    mem_util: float = 0.9
    throttle_ratio: float = 0.5
    entry: bool = False


@dataclass(frozen=True)
class SLOBook:
    services: dict[str, SLO]
    infrastructure: tuple[str, ...] = field(default_factory=tuple)

    def get(self, service: str) -> SLO:
        return self.services.get(service, SLO(service=service))

    @property
    def entry_services(self) -> list[str]:
        return [s.service for s in self.services.values() if s.entry]

    @classmethod
    def load(cls, path: Path) -> SLOBook:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls.from_dict(doc or {})

    @classmethod
    def from_dict(cls, doc: dict) -> SLOBook:
        defaults = doc.get("defaults", {})
        services = {}
        for name, overrides in (doc.get("services") or {}).items():
            services[name] = SLO(service=name, **{**defaults, **(overrides or {})})
        return cls(services=services, infrastructure=tuple(doc.get("infrastructure", [])))
