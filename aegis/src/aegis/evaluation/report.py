"""Aggregate benchmark results into machine-readable and markdown reports."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from aegis.evaluation.stats import brier, duration, rate


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def flags(key: str, subset: list[dict[str, Any]] | None = None) -> list[bool]:
        return [bool(r[key]) for r in (subset if subset is not None else results) if r.get(key) is not None]

    detect_required = [r for r in results if "resolved" in r["outcome_expected"] or "escalated" in r["outcome_expected"]]
    remediable = [r for r in results if "resolved" in r["outcome_expected"] and r["outcome_expected"] == ["resolved"]]
    with_actions = [r for r in results if r["num_actions"] > 0]
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_class[r["fault_class"]].append(r)
    return {
        "runs": len(results), "scenarios": len({r["scenario"] for r in results}),
        "pass_rate": rate(flags("passed")),
        "detection_rate": rate(flags("detected", detect_required)),
        "root_cause_accuracy": rate([bool(r["root_cause_correct"]) for r in results
                                     if r["root_cause_expected"] and r["detected"]]),
        "root_cause_top3": rate([bool(r["root_cause_top3"]) for r in results if r["root_cause_expected"] and r["detected"]]),
        "evidence_grounding": duration([r["evidence_grounding"] for r in results if r.get("evidence_grounding") is not None]),
        "remediation_success_rate": rate([r["remediation_success"] for r in remediable]),
        "first_action_acceptable": rate(flags("first_action_acceptable", with_actions)),
        "unsafe_action_rate": rate(flags("unsafe_action")),
        "false_remediation_rate": rate(flags("false_remediation")),
        "outcome_match_rate": rate(flags("outcome_match")),
        "rollback_success_rate": rate(flags("rollback_success")),
        "human_intervention_rate": rate([r["human_interventions"] > 0 for r in results]),
        "time_to_detect_s": duration([r["time_to_detect_s"] for r in results if r.get("time_to_detect_s") is not None]),
        "detection_latency_s": duration([r["detection_latency_s"] for r in results if r.get("detection_latency_s") is not None]),
        "recovery_time_s": duration([r["recovery_time_s"] for r in results if r.get("recovery_time_s") is not None
                                     and r["outcome"] == "resolved"]),
        "mttr_s": duration([r["mttr_s"] for r in results if r.get("mttr_s") is not None and r["outcome"] == "resolved"]),
        "actions_per_incident": duration([float(r["num_actions"]) for r in results if r["detected"]]),
        "calibration_brier": brier([(r["diagnosis_confidence"], bool(r["root_cause_correct"])) for r in results
                                    if r.get("diagnosis_confidence") is not None and r["root_cause_expected"] and r["detected"]]),
        "by_fault_class": {k: {"runs": len(v), "passed": sum(1 for r in v if r["passed"])} for k, v in sorted(by_class.items())},
    }


def _fmt_rate(x: dict[str, Any]) -> str:
    if x.get("value") is None:
        return "n/a"
    return f"{x['value']:.0%} ({x['k']}/{x['n']}, 95% CI {x['ci95'][0]:.0%}–{x['ci95'][1]:.0%})"


def _fmt_dur(x: dict[str, Any]) -> str:
    if not x.get("n"):
        return "n/a"
    return f"mean {x['mean']}s, median {x['median']}s, p90 {x['p90']}s (n={x['n']}, 95% CI {x['ci95'][0]}–{x['ci95'][1]}s)"


def markdown(meta: dict[str, Any], summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [f"# AegisOps benchmark report — {meta.get('run_id')}", "",
             f"- Mode: **{meta.get('mode')}** · started {meta.get('started_at')} · finished {meta.get('finished_at')}",
             f"- Git: `{meta.get('git_sha')}`{' (dirty)' if meta.get('git_dirty') else ''} · source hash `{meta.get('source_hash')}` · "
             f"config hash `{meta.get('config_hash')}` · policy hash `{meta.get('policy_hash')}`",
             f"- Models: {meta.get('model_routes')}", f"- Repetitions per scenario: {meta.get('repetitions')}", "",
             "## Summary (all numbers are observed, with 95% confidence intervals)", "",
             "| Metric | Result |", "|---|---|"]
    rows = [("Scenario pass rate", _fmt_rate(summary["pass_rate"])),
            ("Detection rate", _fmt_rate(summary["detection_rate"])),
            ("Root-cause accuracy (top-1)", _fmt_rate(summary["root_cause_accuracy"])),
            ("Root-cause accuracy (top-3)", _fmt_rate(summary["root_cause_top3"])),
            ("Remediation success (resolvable scenarios)", _fmt_rate(summary["remediation_success_rate"])),
            ("First action acceptable", _fmt_rate(summary["first_action_acceptable"])),
            ("Unsafe-action rate", _fmt_rate(summary["unsafe_action_rate"])),
            ("False-remediation rate", _fmt_rate(summary["false_remediation_rate"])),
            ("Outcome matches expectation", _fmt_rate(summary["outcome_match_rate"])),
            ("Rollback success", _fmt_rate(summary["rollback_success_rate"])),
            ("Human intervention rate", _fmt_rate(summary["human_intervention_rate"])),
            ("Time to detect (injection → incident)", _fmt_dur(summary["time_to_detect_s"])),
            ("Detection latency (anomaly onset → incident)", _fmt_dur(summary["detection_latency_s"])),
            ("Recovery time (injection → verified resolution)", _fmt_dur(summary["recovery_time_s"])),
            ("MTTR (detection → verified resolution)", _fmt_dur(summary["mttr_s"])),
            ("Actions per detected incident", _fmt_dur(summary["actions_per_incident"])),
            ("Evidence grounding (cited ids that exist)", _fmt_dur(summary["evidence_grounding"]).replace("s", "")),
            ("Confidence calibration (Brier, lower is better)", str(summary["calibration_brier"]))]
    lines += [f"| {k} | {v} |" for k, v in rows]
    lines += ["", "## Per scenario", "",
              "| Scenario | Rep | Detected | TTD s | Root cause | Actions | Outcome | Recovery s | Pass |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rc = "n/a" if r["root_cause_correct"] is None else ("✔" if r["root_cause_correct"] else "✘")
        lines.append(f"| {r['scenario']} | {r['repetition']} | {'yes' if r['detected'] else 'no'} | "
                     f"{r['time_to_detect_s'] if r['time_to_detect_s'] is not None else '–'} | {rc} | "
                     f"{', '.join(r['actions_executed']) or '–'} | {r['outcome']} | "
                     f"{r['recovery_time_s'] if r['recovery_time_s'] is not None else '–'} | {'✔' if r['passed'] else '✘'} |")
    failures = [r for r in results if not r["passed"]]
    if failures:
        lines += ["", "## Failures (unfiltered)", ""]
        for r in failures:
            lines.append(f"- **{r['scenario']}** (rep {r['repetition']}): outcome {r['outcome']} (expected "
                         f"{'/'.join(r['outcome_expected'])}), root cause correct={r['root_cause_correct']}, "
                         f"actions={r['actions_executed'] or 'none'}, notes={'; '.join(r['notes']) or '-'}")
    return "\n".join(lines) + "\n"
