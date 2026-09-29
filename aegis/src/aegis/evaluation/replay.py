"""Offline replay benchmark.

Replays the *exact* evidence bundles captured during live runs through the
diagnosis engine under different configurations (rules only, or rules + a
given model route). Because the inputs are identical, differences in
root-cause accuracy, calibration, latency and cost are attributable to the
configuration, not to run-to-run environmental noise.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from aegis.ai.router import ModelRouter
from aegis.chaos.scenarios import Scenario
from aegis.engine.context import ContextBundle
from aegis.engine.diagnosis.engine import DiagnosisEngine
from aegis.evaluation.stats import brier, duration, rate


def load_cases(run_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(run_dir.glob("*-rep*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        snaps = [s for s in (doc.get("snapshots") or []) if s["label"].startswith("context-round")]
        if not snaps or not doc.get("incident"):
            continue
        cases.append({"file": path.name, "scenario": doc["metrics"]["scenario"], "incident": doc["incident"],
                      "bundle": snaps[0]["data"]})
    return cases


async def replay(run_dir: Path, scenarios: dict[str, Scenario], configs: dict[str, str | None]) -> dict[str, Any]:
    cases = load_cases(run_dir)
    out: dict[str, Any] = {"run_dir": str(run_dir), "cases": len(cases), "configs": {}}
    for name, routes in configs.items():
        router = None
        if routes:
            router = ModelRouter(routes=json.loads(routes), timeout=45.0, max_calls_per_incident=4)
        engine = DiagnosisEngine(router)
        rows = []
        for case in cases:
            sc = scenarios.get(case["scenario"])
            if sc is None or sc.expected.root_cause is None:
                continue
            bundle = ContextBundle.from_dict(case["bundle"])
            t0 = time.perf_counter()
            dx = await engine.diagnose(case["incident"]["id"], case["incident"]["title"], bundle, 1)
            latency = time.perf_counter() - t0
            rc = sc.expected.root_cause
            ok = bool(dx.selected and dx.selected.category.value in rc.categories and dx.selected.component == rc.component)
            top3 = any(h.category.value in rc.categories and h.component == rc.component for h in dx.hypotheses[:3])
            model = dx.model_metadata.get("model") or {}
            rows.append({"scenario": case["scenario"], "correct": ok, "top3": top3, "confidence": dx.confidence,
                         "method": dx.method, "latency_s": round(latency, 3),
                         "selected": f"{dx.selected.category.value}:{dx.selected.component}" if dx.selected else None,
                         "prompt_tokens": model.get("prompt_tokens", 0), "completion_tokens": model.get("completion_tokens", 0),
                         "cost_usd": model.get("cost_usd", 0.0), "model_reason": model.get("reason")})
        out["configs"][name] = {
            "routes": routes, "accuracy": rate([r["correct"] for r in rows]), "top3": rate([r["top3"] for r in rows]),
            "brier": brier([(r["confidence"], r["correct"]) for r in rows]),
            "latency_s": duration([r["latency_s"] for r in rows]),
            "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows),
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6),
            "model_used": sum(1 for r in rows if r["method"] == "rules+model"), "rows": rows}
        if router is not None:
            await router.aclose()
    return out
