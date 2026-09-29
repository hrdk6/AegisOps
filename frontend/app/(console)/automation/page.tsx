"use client";

import clsx from "clsx";
import { Check, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { ApprovalPanel } from "@/components/incident/Panels";
import { Chip, Empty, ErrorNote, Field, Loading, Mono, Panel, Phase, Risk } from "@/components/ui";
import { api, can, fetcher } from "@/lib/api";
import { ago } from "@/lib/format";
import type { Approval } from "@/lib/types";

type RegistryEntry = { type: string; description: string; baseRisk: number; targetKinds: string[]; reversible: boolean;
  simulatable: boolean; disruptive: boolean; prohibited: boolean };
type PolicyDoc = {
  source: string; generation: number; circuitBreaker: { state: string; recentFailures?: number }; registry: RegistryEntry[];
  spec: {
    mode: string; allowedNamespaces: string[]; protectedWorkloads?: { namespace: string; name: string; mode: string }[];
    autonomyMinConfidence: number; approvalRiskThreshold: string; allowSimulationAutoApproval: boolean;
    limits: Record<string, string | number>; approvalTtlSeconds: number; executionTimeoutSeconds: number; autoRevertOnFailure: boolean;
    budgets: { maxActionsPerIncident: number; maxRevertsPerIncident: number; targetCooldownSeconds: number;
      globalMaxActionsPerWindow: number; globalWindowSeconds: number;
      circuitBreaker: { failureThreshold: number; windowSeconds: number; openSeconds: number } };
    actionRules?: { actionType: string; enabled?: boolean; requireApproval?: boolean; minRisk?: string }[];
  };
};
type DecisionRow = { action_id: string; incident_id: string; action_type: string; target: string; phase: string; allowed: boolean | null;
  requires_approval: boolean | null; risk_level: string | null; risk_score: number | null; reasons: string[];
  checks: { name: string; passed: boolean; detail?: string }[]; created_at: string };

const MODES = [
  { id: "observe", label: "Observe", help: "Detect and diagnose only. Every action is denied." },
  { id: "supervised", label: "Supervised", help: "Every action needs a human approval." },
  { id: "autonomous", label: "Autonomous", help: "Low-risk actions run; medium risk runs only with an improved sandbox result." },
];

export default function AutomationPage() {
  const { data: pol, error, mutate: refreshPolicy } = useSWR<PolicyDoc>("/api/v1/policy", fetcher);
  const { data: approvals, mutate: refreshApprovals } = useSWR<{ items: Approval[] }>("/api/v1/approvals", fetcher);
  const { data: decisions } = useSWR<{ items: DecisionRow[] }>("/api/v1/policy/decisions?limit=40", fetcher);
  const [modeBusy, setModeBusy] = useState<string | null>(null);
  const [modeErr, setModeErr] = useState<unknown>(null);

  async function setMode(mode: string) {
    setModeBusy(mode);
    setModeErr(null);
    try {
      await api("/api/v1/policy/mode", { method: "PUT", json: { mode } });
      await refreshPolicy();
    } catch (e) {
      setModeErr(e);
    } finally {
      setModeBusy(null);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (!pol) return <Loading label="Loading policy" />;
  const spec = pol.spec;
  const pending = approvals?.items.filter((a) => a.status === "pending") ?? [];
  const decided = approvals?.items.filter((a) => a.status !== "pending").slice(0, 10) ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="font-display text-xl font-semibold">Automation and policy</h1>
        <p className="mt-1 text-ink2">
          Every action passes the controller&apos;s deterministic policy engine. Models can propose; only this policy can permit.
        </p>
      </div>

      <Panel title="Automation mode" action={<span className="text-xs text-muted">{pol.source} · generation {pol.generation}</span>}>
        <div role="radiogroup" aria-label="Automation mode" className="grid gap-3 md:grid-cols-3">
          {MODES.map((m) => {
            const active = spec.mode === m.id;
            return (
              <button key={m.id} role="radio" aria-checked={active} disabled={!can("policy_admin") || active || !!modeBusy}
                onClick={() => setMode(m.id)}
                className={clsx("rounded-lg border p-3 text-left disabled:cursor-default",
                  active ? "border-accent bg-accent/10" : "border-line hover:border-ink2/50")}>
                <div className="flex items-center justify-between">
                  <span className="font-display font-semibold">{m.label}</span>
                  {active ? <Chip tone="accent">active</Chip> : modeBusy === m.id ? <span className="text-xs text-muted">switching…</span> : null}
                </div>
                <p className="mt-1 text-sm text-ink2">{m.help}</p>
              </button>
            );
          })}
        </div>
        {!can("policy_admin") && <p className="mt-2 text-xs text-muted">Changing the mode requires the admin role. Mode changes are HMAC-signed and audited.</p>}
        {modeErr ? <div className="mt-2"><ErrorNote error={modeErr} /></div> : null}
      </Panel>

      <div id="approvals" className="grid gap-5 xl:grid-cols-2">
        <Panel title={`Pending approvals (${pending.length})`} className="min-w-0">
          {!approvals ? <Loading /> : pending.length === 0 ? <Empty>No actions are waiting for a human.</Empty> : (
            <div className="space-y-5">
              {pending.map((a) => (
                <div key={a.id} className="rounded border border-serious/50 p-3">
                  <Link href={`/incidents/${a.incident_id}`} className="text-sm font-medium hover:text-accent">
                    {a.incident_title ?? a.incident_id}</Link>
                  <p className="mb-2 text-xs text-muted">requested {ago(a.requested_at)} · <Mono>{a.id}</Mono></p>
                  <ApprovalPanel approval={a} canApprove={can("approve")} onDone={() => refreshApprovals()} />
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Recent approval decisions" dense className="min-w-0">
          {decided.length === 0 ? <Empty>No decisions yet.</Empty> : (
            <ul className="divide-y divide-line">
              {decided.map((a) => (
                <li key={a.id} className="px-4 py-2.5 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Mono>{a.context.candidate?.action_type}</Mono><span className="text-ink2">on {a.context.candidate?.target.name}</span>
                    <Chip tone={a.status === "approved" ? "good" : a.status === "rejected" ? "critical" : "neutral"}>{a.status}</Chip>
                    <span className="ml-auto text-xs text-muted">{ago(a.decided_at ?? a.requested_at)}</span>
                  </div>
                  <p className="text-xs text-muted">{a.decided_by ? `by ${a.decided_by}` : ""}{a.reason ? `: ${a.reason}` : ""}</p>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Panel title="Guardrails">
          <dl className="grid grid-cols-2 gap-3">
            <Field label="Managed namespaces">{spec.allowedNamespaces.join(", ") || "none"}</Field>
            <Field label="Autonomy needs confidence">{spec.autonomyMinConfidence}%</Field>
            <Field label="Approval from risk">{spec.approvalRiskThreshold}</Field>
            <Field label="Simulation-gated autonomy">{spec.allowSimulationAutoApproval ? "on" : "off"}</Field>
            <Field label="Actions per incident">{spec.budgets.maxActionsPerIncident}</Field>
            <Field label="Reverts per incident">{spec.budgets.maxRevertsPerIncident}</Field>
            <Field label="Target cooldown">{spec.budgets.targetCooldownSeconds}s</Field>
            <Field label="Global rate">{spec.budgets.globalMaxActionsPerWindow} per {spec.budgets.globalWindowSeconds}s</Field>
            <Field label="Approval expires after">{spec.approvalTtlSeconds}s</Field>
            <Field label="Auto-revert on failure">{spec.autoRevertOnFailure ? "yes" : "no"}</Field>
          </dl>
        </Panel>
        <Panel title="Circuit breaker">
          <div className="flex items-center gap-2">
            <Chip tone={pol.circuitBreaker.state === "open" ? "critical" : pol.circuitBreaker.state === "closed" ? "good" : "neutral"}>
              {pol.circuitBreaker.state}</Chip>
            <span className="text-sm text-ink2">{pol.circuitBreaker.recentFailures ?? 0} recent failed or reverted actions</span>
          </div>
          <p className="mt-2 text-sm text-ink2">
            Opens after {spec.budgets.circuitBreaker.failureThreshold} failures within {spec.budgets.circuitBreaker.windowSeconds}s and stays
            open {spec.budgets.circuitBreaker.openSeconds}s. While open, every action requires human approval.
          </p>
        </Panel>
        <Panel title="Protected workloads">
          {!spec.protectedWorkloads?.length ? <Empty>None.</Empty> : (
            <ul className="space-y-1.5 text-sm">
              {spec.protectedWorkloads.map((p) => (
                <li key={p.name} className="flex items-center justify-between">
                  <span>{p.namespace}/{p.name}</span>
                  <Chip tone={p.mode === "deny" ? "critical" : "serious"}>{p.mode === "deny" ? "never automated" : "approval required"}</Chip>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <Panel title="Recent policy decisions" dense>
        {!decisions ? <Loading /> : decisions.items.length === 0 ? <Empty>No actions evaluated yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-2 font-normal">Action</th><th className="px-2 font-normal">Target</th>
                <th className="px-2 font-normal">Verdict</th><th className="px-2 font-normal">Risk</th>
                <th className="px-2 font-normal">Phase</th><th className="px-2 font-normal">Failed checks / reasons</th>
                <th className="px-4 text-right font-normal">When</th></tr></thead>
              <tbody>
                {decisions.items.map((d) => {
                  const failed = d.checks.filter((c) => !c.passed);
                  return (
                    <tr key={d.action_id} className="border-b border-line/60 align-top">
                      <td className="px-4 py-2"><Mono>{d.action_type}</Mono>
                        <Link href={`/incidents/${d.incident_id}`} className="block text-xs text-muted hover:text-accent">{d.incident_id}</Link></td>
                      <td className="px-2">{d.target}</td>
                      <td className="px-2">{d.allowed === false ? <Chip tone="critical">denied</Chip> : d.requires_approval
                        ? <Chip tone="serious">approval</Chip> : d.allowed ? <Chip tone="good">allowed</Chip> : "–"}</td>
                      <td className="px-2"><Risk level={d.risk_level} />{d.risk_score !== null && <span className="ml-1 text-xs text-muted">{d.risk_score}</span>}</td>
                      <td className="px-2"><Phase phase={d.phase} /></td>
                      <td className="max-w-md px-2 text-xs text-ink2">
                        {failed.length ? failed.map((c) => <div key={c.name}><X size={11} className="mr-1 inline text-critical" aria-hidden />
                          <span className="font-mono">{c.name}</span> {c.detail}</div>)
                          : d.reasons.length ? d.reasons.join("; ") : <span className="text-muted"><Check size={11} className="mr-1 inline text-good" aria-hidden />all checks passed</span>}
                      </td>
                      <td className="px-4 text-right text-xs text-muted">{ago(d.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Action registry" dense>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-2 font-normal">Action</th><th className="px-2 font-normal">Description</th>
              <th className="px-2 text-right font-normal">Base risk</th><th className="px-2 font-normal">Targets</th>
              <th className="px-4 font-normal">Properties</th></tr></thead>
            <tbody>
              {pol.registry.map((r) => (
                <tr key={r.type} className={clsx("border-b border-line/60", r.prohibited && "text-muted")}>
                  <td className="px-4 py-1.5"><Mono className={r.prohibited ? "line-through" : ""}>{r.type}</Mono></td>
                  <td className="px-2 text-ink2">{r.description}</td>
                  <td className="tabular px-2 text-right">{r.prohibited ? "–" : r.baseRisk}</td>
                  <td className="px-2 text-xs">{r.targetKinds?.join(", ") || "–"}</td>
                  <td className="px-4">
                    <div className="flex flex-wrap gap-1">
                      {r.prohibited ? <Chip tone="critical">prohibited</Chip> : <>
                        {r.reversible && <Chip tone="good" icon={false}>reversible</Chip>}
                        {r.simulatable && <Chip tone="accent" icon={false}>simulatable</Chip>}
                        {r.disruptive && <Chip tone="warning" icon={false}>disruptive</Chip>}
                      </>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <p className="text-xs text-muted">Button states reflect your role; the API and controller enforce the same rules server-side.</p>
    </div>
  );
}
