"""Incident scenario definitions shared by the fault injector and the benchmark."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fault(Strict):
    type: Literal["release", "env", "resources", "scale", "configmap", "network", "traffic", "pod_kill", "canary"]
    target: str
    image_tag: str | None = None
    version: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    replicas: int | None = Field(default=None, ge=0, le=10)
    configmap: str | None = None
    data: dict[str, str] = Field(default_factory=dict)
    restart: bool = True
    proxy: str | None = None
    toxic: dict[str, Any] = Field(default_factory=dict)
    traffic: dict[str, float] = Field(default_factory=dict)
    canary: dict[str, Any] = Field(default_factory=dict)


class RemediationRef(Strict):
    action: str
    target: str

    def matches(self, action: str, target: str) -> bool:
        return self.action in ("*", action) and self.target in ("*", target)


class Remediation(Strict):
    optimal: list[RemediationRef] = Field(default_factory=list)
    acceptable: list[RemediationRef] = Field(default_factory=list)
    prohibited: list[RemediationRef] = Field(default_factory=list)


class RootCause(Strict):
    categories: list[str]
    component: str


class Expected(Strict):
    detection: Literal["required", "optional", "none"] = "required"
    symptoms: list[str] = Field(default_factory=list)
    root_cause: RootCause | None = None
    remediation: Remediation = Field(default_factory=Remediation)
    outcomes: list[Literal["resolved", "escalated", "self_recovered", "auto_mitigated", "no_incident"]]


class VerificationSpec(Strict):
    services: list[str]
    max_error_ratio: float = 0.02
    max_p95_ms: float = 900


class Timeouts(Strict):
    detect: int = 150
    resolve: int = 600


class Scenario(Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{3,64}$")
    title: str
    fault_class: str
    description: str
    environment: str = "ShopFlow baseline: all services healthy, default synthetic traffic"
    fault: Fault
    expected: Expected
    verification: VerificationSpec
    rollback_conditions: str = ""
    timeouts: Timeouts = Field(default_factory=Timeouts)
    demo: bool = False
    tags: list[str] = Field(default_factory=list)


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.yaml")):
        sc = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if sc.id in out:
            raise ValueError(f"duplicate scenario id {sc.id}")
        out[sc.id] = sc
    return out
