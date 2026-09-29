"""`aegis` — command-line interface for operators and developers."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from aegis.cli.client import ApiClient, ApiError, load_config, save_config

app = typer.Typer(help="AegisOps: autonomous reliability control plane CLI.", no_args_is_help=True)
incident_app = typer.Typer(help="Inspect and operate incidents.", no_args_is_help=True)
remediation_app = typer.Typer(help="Remediation plans and actions.", no_args_is_help=True)
policy_app = typer.Typer(help="Safety policy.", no_args_is_help=True)
scenario_app = typer.Typer(help="Demo fault scenarios (isolated demo environment only).", no_args_is_help=True)
benchmark_app = typer.Typer(help="Benchmark AegisOps against reproducible incidents.", no_args_is_help=True)
system_app = typer.Typer(help="Platform health.", no_args_is_help=True)
topology_app = typer.Typer(help="Service dependency graph queries.", no_args_is_help=True)
for sub, name in ((incident_app, "incident"), (remediation_app, "remediation"), (policy_app, "policy"),
                  (scenario_app, "scenario"), (benchmark_app, "benchmark"), (system_app, "system"), (topology_app, "topology")):
    app.add_typer(sub, name=name)

console = Console()
T = TypeVar("T")
STATUS_STYLE = {"healthy": "green", "degraded": "yellow", "down": "red", "RESOLVED": "green", "ESCALATED": "red",
                "AWAITING_APPROVAL": "magenta", "EXECUTING": "cyan", "VERIFYING": "cyan", "ok": "green"}


def run(fn: Callable[[ApiClient], Awaitable[T]], url: str | None = None) -> T:
    async def main() -> T:
        api = ApiClient.from_config(url)
        try:
            return await fn(api)
        finally:
            await api.close()

    try:
        return asyncio.run(main())
    except ApiError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def styled(v: Any) -> str:
    s = str(v)
    return f"[{STATUS_STYLE[s]}]{s}[/]" if s in STATUS_STYLE else s


def emit(data: Any, as_json: bool) -> bool:
    if as_json:
        console.print_json(json.dumps(data, default=str))
    return as_json


JSON = typer.Option(False, "--json", help="Print raw JSON.")


@app.command()
def login(username: str = typer.Option(..., prompt=True), url: str = typer.Option("http://localhost:8000", envvar="AEGIS_API_URL"),
          password: str = typer.Option(..., prompt=True, hide_input=True, envvar="AEGIS_PASSWORD")) -> None:
    """Authenticate and store a session token (~/.aegis/cli.json)."""
    async def go(api: ApiClient) -> dict[str, Any]:
        return await api.login(username, password)

    body = run(go, url)
    cfg = load_config()
    cfg.update({"url": url, "token": body["access_token"], "user": body["user"]})
    save_config(cfg)
    console.print(f"logged in as [bold]{body['user']['username']}[/] ({body['user']['role']})")


@app.command()
def status(as_json: bool = JSON) -> None:
    """Overall system status: services, incidents, automation mode."""
    data = run(lambda api: api.get("/api/v1/overview"))
    if emit(data, as_json):
        return
    auto = data["automation"]
    console.print(f"automation mode: [bold]{auto.get('mode')}[/] · circuit breaker: {auto.get('breaker', {}).get('state')} · "
                  f"pending approvals: {data['pending_approvals']} · SLO compliance: {data['slo_compliance']}")
    t = Table("service", "status", "rps", "5xx", "p95 ms", "cpu", "mem", "anomalies")
    for s in data["services"]:
        sig = s["signals"]
        t.add_row(s["name"], styled(s["status"]), str(sig.get("rps")), str(sig.get("error_ratio")), str(sig.get("p95_ms")),
                  str(sig.get("cpu_util")), str(sig.get("mem_util")), ", ".join(a["signal"] for a in sig.get("anomalies", [])))
    console.print(t)
    if data["active_incidents"]:
        console.print("[bold]active incidents[/]")
        for i in data["active_incidents"]:
            console.print(f"  {i['id']} {styled(i['status'])} {i['severity']} {i['title']}")


@app.command()
def incidents(status: str = typer.Option(None, help="active | RESOLVED | ESCALATED | ..."), limit: int = 20,
              as_json: bool = JSON) -> None:
    """List incidents."""
    data = run(lambda api: api.get("/api/v1/incidents", status=status, limit=limit))
    if emit(data, as_json):
        return
    t = Table("id", "sev", "status", "title", "root cause", "outcome", "duration s")
    for i in data["items"]:
        rc = f"{i['category']} on {i['root_service']}" if i.get("category") else "-"
        t.add_row(i["id"], i["severity"], styled(i["status"]), i["title"], rc, i.get("outcome") or "-",
                  f"{i['duration_seconds']:.0f}")
    console.print(t)


@incident_app.command("get")
def incident_get(incident_id: str, as_json: bool = JSON) -> None:
    """Incident detail: diagnosis, remediation, verification."""
    d = run(lambda api: api.get(f"/api/v1/incidents/{incident_id}"))
    if emit(d, as_json):
        return
    console.print(f"[bold]{d['id']}[/] {d['severity']} {styled(d['status'])} — {d['title']}")
    console.print(f"affected: {', '.join(d['affected_services'])} · outcome: {d.get('outcome') or '-'}")
    dx = d.get("diagnosis")
    if dx:
        console.print(f"\n[bold]diagnosis[/] ({dx['method']}, confidence {dx['confidence']:.0%}): {dx['summary']}")
        for h in dx["hypotheses"][:4]:
            console.print(f"  {h['confidence']:.0%}  {h['category']} on {h['component']} — {h['statement']}")
        console.print(f"  uncertainty: {dx['uncertainty']}")
    for a in d["actions"]:
        console.print(f"[bold]action[/] {a['id']}: {a['action_type']} {a['target'].get('name')} {a['params'] or ''} "
                      f"→ {styled(a['phase'])} (risk {a['risk_level']}, verification {a.get('outcome') or '-'})")
    for v in d["verifications"]:
        console.print(f"[bold]verification[/] {v['summary']}")


@incident_app.command("timeline")
def incident_timeline(incident_id: str, as_json: bool = JSON) -> None:
    data = run(lambda api: api.get(f"/api/v1/incidents/{incident_id}/timeline"))
    if emit(data, as_json):
        return
    for e in data["items"]:
        console.print(f"{e['ts'][11:19]}  [dim]{e['type']:<22}[/] {e['message']}")


@incident_app.command("evidence")
def incident_evidence(incident_id: str, kind: str = typer.Option(None), as_json: bool = JSON) -> None:
    data = run(lambda api: api.get(f"/api/v1/incidents/{incident_id}/evidence", kind=kind))
    if emit(data, as_json):
        return
    for e in data["items"]:
        console.print(f"[bold]{e['id']}[/] [{e['kind']}] score {e['score']:.2f} {e['title']}\n    source: {e['source'].get('system')}"
                      f" {e['source'].get('query') or ''}"[:220])


@incident_app.command("investigate")
def incident_investigate(incident_id: str) -> None:
    """Request (re-)investigation."""
    console.print(run(lambda api: api.post(f"/api/v1/incidents/{incident_id}/investigate")))


@incident_app.command("resolve")
def incident_resolve(incident_id: str, note: str = typer.Option("", help="resolution note")) -> None:
    console.print(run(lambda api: api.post(f"/api/v1/incidents/{incident_id}/resolve", {"note": note})))


@incident_app.command("escalate")
def incident_escalate(incident_id: str, note: str = typer.Option("")) -> None:
    console.print(run(lambda api: api.post(f"/api/v1/incidents/{incident_id}/escalate", {"note": note})))


@incident_app.command("postmortem")
def incident_postmortem(incident_id: str, raw: bool = typer.Option(False, help="print raw markdown")) -> None:
    md = run(lambda api: api.request("GET", f"/api/v1/incidents/{incident_id}/postmortem", params={"format": "markdown"},
                                     raw=True))
    console.print(md if raw else Markdown(md))


@remediation_app.command("list")
def remediation_list(incident_id: str, as_json: bool = JSON) -> None:
    """Remediation candidates of the latest plan with policy verdicts."""
    data = run(lambda api: api.get(f"/api/v1/incidents/{incident_id}/plans"))
    if emit(data, as_json):
        return
    for plan in data["items"][-1:]:
        t = Table("#", "action", "target", "params", "risk", "allowed", "approval", "utility", "rationale")
        for i, c in enumerate(plan["candidates"]):
            mark = "★" if i == plan.get("selected_index") else str(i)
            t.add_row(mark, c["action_type"], c["target"]["name"], json.dumps(c["params"]), str(c.get("risk_level")),
                      str(c.get("policy_allowed")), str(c.get("requires_approval")), str(c.get("utility")), c["rationale"][:80])
        console.print(t)
        if plan.get("escalation_reason"):
            console.print(f"escalation: {plan['escalation_reason']}")


@app.command()
def approvals(status: str = typer.Option("pending"), as_json: bool = JSON) -> None:
    data = run(lambda api: api.get("/api/v1/approvals", status=status))
    if emit(data, as_json):
        return
    for a in data["items"]:
        c = a["context"].get("candidate", {})
        console.print(f"[bold]{a['id']}[/] ({a['incident_id']}) {c.get('action_type')} {c.get('target', {}).get('name')} "
                      f"risk {c.get('risk_level')} — {a['status']}\n    {c.get('rationale', '')}")


@app.command()
def approve(action_id: str, reason: str = typer.Option("", help="justification")) -> None:
    """Approve a pending action (requires the approver role)."""
    console.print(run(lambda api: api.post(f"/api/v1/approvals/{action_id}/decision", {"decision": "approved", "reason": reason})))


@app.command()
def reject(action_id: str, reason: str = typer.Option("", help="justification")) -> None:
    console.print(run(lambda api: api.post(f"/api/v1/approvals/{action_id}/decision", {"decision": "rejected", "reason": reason})))


@policy_app.command("show")
def policy_show(as_json: bool = JSON) -> None:
    data = run(lambda api: api.get("/api/v1/policy"))
    if emit(data, as_json):
        return
    spec = data["spec"]
    console.print(f"source {data['source']} · mode [bold]{spec['mode']}[/] · breaker {data['circuitBreaker']}")
    console.print(f"budgets: {spec['budgets']}")
    t = Table("action", "base risk", "reversible", "simulatable", "prohibited")
    for r in data["registry"]:
        t.add_row(r["type"], str(r["baseRisk"]), str(r["reversible"]), str(r["simulatable"]),
                  "[red]yes[/]" if r["prohibited"] else "no")
    console.print(t)


@policy_app.command("check")
def policy_check(action: str, target: str, param: list[str] = typer.Option([], help="key=value (replicas=3)"),
                 kind: str = "Deployment", confidence: int = 80, as_json: bool = JSON) -> None:
    """Dry-run the deterministic policy engine for a hypothetical action."""
    params: dict[str, Any] = {}
    for p in param:
        k, _, v = p.partition("=")
        params[k] = int(v) if v.isdigit() else v
    data = run(lambda api: api.post("/api/v1/policy/check", {"action_type": action, "target": target, "target_kind": kind,
                                                               "parameters": params, "diagnosis_confidence": confidence}))
    if emit(data, as_json):
        return
    d = data["decision"]
    verdict = "[red]DENIED[/]" if not d["allowed"] else ("[yellow]APPROVAL REQUIRED[/]" if d["requiresApproval"] else "[green]ALLOWED[/]")
    console.print(f"{verdict} · risk {d['riskLevel']} ({d['riskScore']})")
    for c in d["checks"]:
        console.print(f"  {'✔' if c['passed'] else '✘'} {c['name']}: {c.get('detail', '')}")


@policy_app.command("mode")
def policy_mode(mode: str = typer.Argument(..., help="autonomous | supervised | observe")) -> None:
    console.print(run(lambda api: api.put("/api/v1/policy/mode", {"mode": mode})))


@scenario_app.command("list")
def scenario_list(as_json: bool = JSON) -> None:
    data = run(lambda api: api.get("/api/v1/demo/scenarios"))
    if emit(data, as_json):
        return
    t = Table("id", "class", "fault", "target", "title")
    for s in data["items"]:
        t.add_row(s["id"], s["fault_class"], s["fault_type"], s["target"], s["title"])
    console.print(t)


@scenario_app.command("inject")
def scenario_inject(scenario_id: str) -> None:
    """Inject a catalogued fault into the isolated demo namespace."""
    console.print(run(lambda api: api.post(f"/api/v1/demo/scenarios/{scenario_id}/inject")))


@scenario_app.command("reset")
def scenario_reset() -> None:
    """Restore the demo namespace to its declared state."""
    console.print(run(lambda api: api.post("/api/v1/demo/reset")))


@scenario_app.command("status")
def scenario_status() -> None:
    console.print(run(lambda api: api.get("/api/v1/demo/status")))


@system_app.command("health")
def system_health(as_json: bool = JSON) -> None:
    data = run(lambda api: api.get("/api/v1/system/health"))
    if emit(data, as_json):
        return
    t = Table("component", "status", "detail")
    for c in data["components"]:
        t.add_row(c["name"], styled(c["status"]), str(c.get("detail") or ""))
    console.print(t)


@topology_app.command("impact")
def topology_impact(service: str) -> None:
    """If SERVICE is unhealthy, which upstream services degrade?"""
    data = run(lambda api: api.get(f"/api/v1/topology/impact/{service}"))
    for b in data["blast_radius"]:
        console.print(f"  {b['service']}  via {' -> '.join(b['path'])}")


@topology_app.command("explain")
def topology_explain(service: str) -> None:
    """Which dependencies could explain a symptom on SERVICE?"""
    data = run(lambda api: api.get(f"/api/v1/topology/explain/{service}"))
    for c in data["candidates"]:
        console.print(f"  {'[red]anomalous[/]' if c['anomalous'] else 'healthy  '}  {c['service']} (distance {c['distance']}) "
                      f"path {' -> '.join(c['path'])}")


@app.command("audit-verify")
def audit_verify() -> None:
    """Verify the hash chain of the audit log."""
    console.print(run(lambda api: api.get("/api/v1/audit/verify")))


def _repo_root() -> Path:
    here = Path.cwd()
    for p in [here, *here.parents]:
        if (p / "benchmarks" / "scenarios").exists():
            return p
    return here


@benchmark_app.command("run")
def benchmark_run(scenario: list[str] = typer.Option([], help="scenario id (repeatable); default all"),
                  repeat: int = typer.Option(1, min=1, max=20), approve: bool = typer.Option(True),
                  output: Path = typer.Option(None, help="results directory")) -> None:
    """Run the live benchmark against the running environment."""
    from aegis.chaos.scenarios import load_scenarios
    from aegis.evaluation.runner import LiveRunner

    root = _repo_root()
    scenarios = load_scenarios(root / "benchmarks" / "scenarios")
    ids = scenario or list(scenarios)
    unknown = [i for i in ids if i not in scenarios]
    if unknown:
        console.print(f"[red]unknown scenarios:[/] {unknown}")
        raise typer.Exit(2)

    async def go(api: ApiClient) -> Path:
        runner = LiveRunner(api, scenarios, output or root / "benchmarks" / "results", root, repeat, approve,
                            log=lambda m: console.print(m, markup=False, highlight=False))
        return await runner.run(ids)

    out = run(go)
    console.print(f"[green]benchmark complete[/]: {out / 'report.md'}")
    console.print(Markdown((out / "report.md").read_text(encoding="utf-8").split("## Per scenario")[0]))


@benchmark_app.command("replay")
def benchmark_replay(run_dir: Path, config: list[str] = typer.Option(
        [], help="name=ROUTES_JSON or name=rules (repeatable); default: rules only")) -> None:
    """Replay captured evidence bundles through the diagnosis engine under different configurations."""
    from aegis.chaos.scenarios import load_scenarios
    from aegis.evaluation.replay import replay

    configs: dict[str, str | None] = {"rules": None}
    for c in config:
        name, _, routes = c.partition("=")
        configs[name] = None if routes in ("", "rules") else routes
    scenarios = load_scenarios(_repo_root() / "benchmarks" / "scenarios")
    result = asyncio.run(replay(run_dir, scenarios, configs))
    (run_dir / "replay.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    t = Table("config", "cases", "top-1 accuracy (95% CI)", "top-3", "Brier", "mean latency s", "tokens", "cost $")
    for name, r in result["configs"].items():
        acc = r["accuracy"]
        t.add_row(name, str(acc["n"]), f"{acc['value']} {acc['ci95']}", str(r["top3"]["value"]), str(r["brier"]),
                  str(r["latency_s"]["mean"]), str(r["tokens"]), str(r["cost_usd"]))
    console.print(t)


@benchmark_app.command("report")
def benchmark_report(run_dir: Path) -> None:
    console.print(Markdown((run_dir / "report.md").read_text(encoding="utf-8")))


@benchmark_app.command("list")
def benchmark_list() -> None:
    data = run(lambda api: api.get("/api/v1/evaluations"))
    for r in data["items"]:
        s = r.get("summary") or {}
        console.print(f"{r['id']} {r['status']} pass={s.get('pass_rate', {}).get('value')} "
                      f"rc={s.get('root_cause_accuracy', {}).get('value')}")


if __name__ == "__main__":
    sys.exit(app())

