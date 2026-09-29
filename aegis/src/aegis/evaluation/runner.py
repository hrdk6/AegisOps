"""Live benchmark: inject each scenario into the running environment, let AegisOps
handle it end to end, then score the outcome against the scenario's ground truth.

Approvals: when AegisOps asks for approval, the runner approves as the
benchmark approver (a policy-compliant human stand-in). Each approval is
counted as a human intervention; it measures proposal quality, not autonomy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.chaos.scenarios import Scenario
from aegis.cli.client import ApiClient, ApiError
from aegis.evaluation.report import markdown, summarize
from aegis.evaluation.scoring import Observation, score

TERMINAL = {"RESOLVED", "ESCALATED"}
SOURCE_DIRS = ["controlplane/api", "controlplane/cmd", "controlplane/internal", "aegis/src", "shopflow/src", "config",
               "knowledge", "benchmarks/scenarios", "deploy/base"]


def source_hash(root: Path) -> str:
    h = hashlib.sha256()
    for d in SOURCE_DIRS:
        for p in sorted((root / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc",):
                h.update(str(p.relative_to(root)).replace("\\", "/").encode())
                h.update(p.read_bytes())
    return h.hexdigest()[:16]


def git_info(root: Path) -> tuple[str, bool]:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10,
                             check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        sha = "uncommitted"
    try:
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                                    timeout=10).stdout.strip())
    except (subprocess.SubprocessError, OSError):
        dirty = True
    return sha, dirty


class LiveRunner:
    def __init__(self, api: ApiClient, scenarios: dict[str, Scenario], out_root: Path, repo_root: Path,
                 repetitions: int = 1, approve: bool = True, log: Callable[[str], None] = print) -> None:
        self.api = api
        self.scenarios = scenarios
        self.out_root = out_root
        self.repo_root = repo_root
        self.repetitions = repetitions
        self.approve = approve
        self.log = log

    async def metadata(self, run_id: str) -> dict[str, Any]:
        sha, dirty = git_info(self.repo_root)
        health = await self.api.get("/api/v1/system/health")
        engine = next((c for c in health["components"] if c["name"] == "engine"), {})
        details = engine.get("details") or {}
        policy = await self.api.get("/api/v1/policy")
        return {"run_id": run_id, "mode": "live", "started_at": datetime.now(UTC).isoformat(), "git_sha": sha,
                "git_dirty": dirty, "source_hash": source_hash(self.repo_root),
                "config_hash": details.get("config_fingerprint"),
                "policy_hash": hashlib.sha256(json.dumps(policy.get("spec"), sort_keys=True).encode()).hexdigest()[:16],
                "policy_mode": policy.get("spec", {}).get("mode"), "model_routes": details.get("llm_routes"),
                "repetitions": self.repetitions, "approver": "benchmark-approver" if self.approve else "none"}

    async def _services_healthy(self) -> bool:
        items = (await self.api.get("/api/v1/services"))["items"]
        return all(s["status"] == "healthy" for s in items if s.get("replicas"))

    async def _close_active(self, reason: str) -> list[str]:
        notes = []
        for inc in (await self.api.get("/api/v1/incidents", status="active"))["items"]:
            if inc["status"] not in ("EXECUTING", "VERIFYING"):
                await self.api.post(f"/api/v1/incidents/{inc['id']}/resolve", {"note": reason})
                notes.append(f"closed leftover incident {inc['id']} ({reason})")
        return notes

    async def stabilize(self, timeout: float = 420) -> list[str]:
        """Reset the environment and wait for 60s of sustained health with no open incident.

        Leftovers from the previous scenario are closed once, before the reset. Incidents that open *during*
        stabilization (reactions to the reset itself) are left to the engine; they are only force-closed
        if still open at the timeout, so the runner never fights the detector in a close/re-open loop.
        """
        notes = await self._close_active("benchmark: previous scenario finished")
        await self.api.post("/api/v1/demo/reset")
        deadline = time.monotonic() + timeout
        streak = 0
        while time.monotonic() < deadline:
            active = (await self.api.get("/api/v1/incidents", status="active"))["items"]
            healthy = await self._services_healthy()
            streak = streak + 1 if healthy and not active else 0
            # 60s of sustained health: detector baselines settle and any escalation hold on the previous
            # incident (escalation_hold_seconds) lapses, so the next fault opens its own incident.
            if streak >= 12:
                return notes
            await asyncio.sleep(5)
        notes += await self._close_active("benchmark: environment did not stabilize")
        notes.append("environment did not fully stabilize before injection")
        return notes

    async def _find_incident(self, sc: Scenario, since: datetime) -> dict[str, Any] | None:
        items = (await self.api.get("/api/v1/incidents", limit=10))["items"]
        for inc in sorted(items, key=lambda i: i["detected_at"]):
            if datetime.fromisoformat(inc["detected_at"]) >= since:
                return inc
        return None

    async def run_one(self, sc: Scenario, rep: int) -> dict[str, Any]:
        notes = await self.stabilize()
        injected_at = datetime.now(UTC)
        await self.api.post(f"/api/v1/demo/scenarios/{sc.id}/inject")
        self.log(f"  injected {sc.id} (rep {rep}) at {injected_at:%H:%M:%S}")
        t0 = time.monotonic()
        incident_id: str | None = None
        approved: set[str] = set()
        while True:
            elapsed = time.monotonic() - t0
            if incident_id is None:
                inc = await self._find_incident(sc, injected_at)
                if inc:
                    incident_id = inc["id"]
                    self.log(f"  detected {incident_id}: {inc['title']} after {elapsed:.0f}s")
                elif elapsed > sc.timeouts.detect:
                    notes.append(f"no incident within {sc.timeouts.detect}s")
                    break
            if incident_id:
                detail = await self.api.get(f"/api/v1/incidents/{incident_id}")
                if self.approve:
                    for ap in detail.get("approvals", []):
                        if ap["status"] == "pending" and ap["id"] not in approved:
                            try:
                                await self.api.post(f"/api/v1/approvals/{ap['id']}/decision",
                                                    {"decision": "approved", "reason": "benchmark approver"})
                                approved.add(ap["id"])
                                self.log(f"  approved {ap['id']}")
                            except ApiError as exc:
                                notes.append(f"approval of {ap['id']} failed: {exc}")
                if detail["status"] in TERMINAL:
                    for _ in range(10):
                        if (await self.api.get(f"/api/v1/incidents/{incident_id}")).get("has_postmortem"):
                            break
                        await asyncio.sleep(2)
                    break
                if elapsed > sc.timeouts.resolve:
                    notes.append(f"incident not closed within {sc.timeouts.resolve}s (status {detail['status']})")
                    break
            await asyncio.sleep(5)
        finished = datetime.now(UTC)
        incident = evidence = snapshots = None
        if incident_id:
            incident = await self.api.get(f"/api/v1/incidents/{incident_id}")
            evidence = (await self.api.get(f"/api/v1/incidents/{incident_id}/evidence"))["items"]
            snapshots = (await self.api.get(f"/api/v1/incidents/{incident_id}/snapshots"))["items"]
        obs = Observation(scenario=sc, repetition=rep, injected_at=injected_at, finished_at=finished, incident=incident,
                          evidence_ids={e["id"] for e in evidence or []},
                          evidence_kinds={e["id"]: e["kind"] for e in evidence or []}, notes=notes)
        metrics = score(obs)
        self.log(f"  -> outcome {metrics['outcome']}, root cause {metrics['root_cause_correct']}, "
                 f"actions {metrics['actions_executed']}, passed={metrics['passed']}")
        return {"metrics": metrics, "incident": incident, "evidence": evidence, "snapshots": snapshots,
                "injected_at": injected_at.isoformat(), "finished_at": finished.isoformat()}

    async def run(self, ids: list[str]) -> Path:
        run_id = f"live-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        out = self.out_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        meta = await self.metadata(run_id)
        await self.api.post("/api/v1/evaluations", {"id": run_id, "mode": "live", "metadata": meta})
        results: list[dict[str, Any]] = []
        for sid in ids:
            sc = self.scenarios[sid]
            for rep in range(1, self.repetitions + 1):
                self.log(f"[{sid}] repetition {rep}/{self.repetitions}")
                try:
                    res = await self.run_one(sc, rep)
                except Exception as exc:  # a broken run must not abort the benchmark
                    self.log(f"  run failed: {exc!r}")
                    res = {"metrics": score(Observation(sc, rep, datetime.now(UTC), datetime.now(UTC), None,
                                                        notes=[f"runner error: {exc!r}"])), "incident": None}
                (out / f"{sid}-rep{rep}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
                results.append(res["metrics"])
                await self.api.post(f"/api/v1/evaluations/{run_id}/results", {
                    "scenario_id": sid, "repetition": rep, "incident_id": (res.get("incident") or {}).get("id"),
                    "passed": res["metrics"]["passed"], "metrics": res["metrics"],
                    "details": {"notes": res["metrics"]["notes"]}, "started_at": res.get("injected_at"),
                    "finished_at": res.get("finished_at")})
        await self.stabilize()
        meta["finished_at"] = datetime.now(UTC).isoformat()
        summary = summarize(results)
        (out / "summary.json").write_text(json.dumps({"metadata": meta, "summary": summary, "results": results},
                                                     indent=2, default=str), encoding="utf-8")
        (out / "report.md").write_text(markdown(meta, summary, results), encoding="utf-8")
        await self.api.post(f"/api/v1/evaluations/{run_id}/complete", {"summary": summary, "metadata": meta})
        return out
