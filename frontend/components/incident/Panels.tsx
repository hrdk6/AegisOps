"use client";

import clsx from "clsx";
import { Check, Star, X } from "lucide-react";
import { useState } from "react";
import { Bar, Button, Chip, Empty, ErrorNote, Mono, Phase, Risk } from "@/components/ui";
import { api } from "@/lib/api";
import { clock, humanize, ms, pct } from "@/lib/format";
import type { ActionView, Approval, Diagnosis, Plan, SimMetrics, Simulation, Verification } from "@/lib/types";

/** Action parameters without unset fields, e.g. {"toRevision":12}. */
export function paramText(params: Record<string, unknown>): string {
  const set = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== null && v !== undefined));
  return Object.keys(set).length ? JSON.stringify(set) : "–";
}

export function DiagnosisPanel({ dx, onCite }: { dx: Diagnosis; onCite: (id: string) => void }) {
  const selectedKey = dx.selected ? `${dx.selected.category}:${dx.selected.component}` : null;
  return (
    <div className="space-y-4">
      <p className="text-md leading-relaxed text-ink">{dx.summary}</p>
      <ol className="space-y-3">
        {dx.hypotheses.map((h) => {
          const selected = `${h.category}:${h.component}` === selectedKey;
          return (
            <li key={`${h.category}:${h.component}`} className={clsx("rounded border p-3", selected ? "border-accent/60 bg-accent/5" : "border-line")}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-display font-semibold">{humanize(h.category)}</span>
                <span className="text-ink2">on {h.component}{h.edge ? ` (path ${h.edge.replace("->", " → ")})` : ""}</span>
                {selected && <Chip tone="accent">Selected</Chip>}
                <span className="ml-auto text-xs text-muted">{h.source === "rules" ? "deterministic rules" : h.source === "model" ? "model only" : "rules + model"}</span>
              </div>
              <div className="mt-2"><Bar value={h.confidence} label={`confidence ${Math.round(h.confidence * 100)}%`} /></div>
              <p className="mt-2 text-sm text-ink2">{h.statement}</p>
              {(h.supporting_evidence.length > 0 || h.contradicting_evidence.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
                  {h.supporting_evidence.map((id) => (
                    <button key={id} onClick={() => onCite(id)} className="rounded border border-good/40 px-1.5 py-px font-mono text-good hover:bg-good/10"
                      title="Supporting evidence">+ {id}</button>
                  ))}
                  {h.contradicting_evidence.map((id) => (
                    <button key={id} onClick={() => onCite(id)} className="rounded border border-critical/40 px-1.5 py-px font-mono text-critical hover:bg-critical/10"
                      title="Contradicting evidence">− {id}</button>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <div className="rounded border border-line bg-raised p-3 text-sm">
        <div className="text-xs text-muted">Uncertainty</div>
        <p className="mt-0.5 text-ink2">{dx.uncertainty}</p>
      </div>
      {dx.blast_radius.length > 0 && (
        <p className="text-sm text-ink2">Blast radius if unresolved: {dx.blast_radius.join(", ")}.</p>
      )}
    </div>
  );
}

function Verdict({ c }: { c: Plan["candidates"][number] }) {
  if (c.policy_allowed === false) return <Chip tone="critical">Denied</Chip>;
  if (c.requires_approval) return <Chip tone="serious">Needs approval</Chip>;
  if (c.policy_allowed) return <Chip tone="good">Allowed</Chip>;
  return <Chip>Not evaluated</Chip>;
}

export function RemediationPanel({ plan }: { plan: Plan }) {
  const sel = plan.selected_index !== null ? plan.candidates[plan.selected_index] : null;
  return (
    <div className="space-y-4">
      {plan.candidates.length === 0 ? (
        <Empty>No candidate actions. {plan.escalation_reason}</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-2 pr-2 font-normal" aria-label="Selected" />
                <th className="py-2 pr-3 font-normal">Action</th><th className="pr-3 font-normal">Target</th>
                <th className="pr-3 font-normal">Parameters</th><th className="pr-3 font-normal">Risk</th>
                <th className="pr-3 font-normal" title="Policy dry run at planning time, before any simulation">Policy (dry run)</th><th className="pr-3 text-right font-normal">P(success)</th>
                <th className="text-right font-normal">Utility</th>
              </tr>
            </thead>
            <tbody>
              {plan.candidates.map((c, i) => (
                <tr key={c.id} className="border-b border-line/60 align-top">
                  <td className="py-2 pr-2">{i === plan.selected_index && <Star size={14} className="text-accent" aria-label="Selected" />}</td>
                  <td className="py-2 pr-3"><Mono>{c.action_type}</Mono>{c.source === "model" && <span className="ml-1 text-xs text-muted">(model)</span>}</td>
                  <td className="pr-3">{c.target.name}</td>
                  <td className="pr-3"><Mono className="text-ink2">{paramText(c.params)}</Mono></td>
                  <td className="pr-3"><Risk level={c.risk_level} /></td>
                  <td className="pr-3"><Verdict c={c} /></td>
                  <td className="tabular pr-3 text-right">{pct(c.confidence, 0)}</td>
                  <td className="tabular text-right">{c.utility.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {sel && (
        <dl className="grid gap-3 rounded border border-line bg-raised p-3 text-sm sm:grid-cols-2">
          <div><dt className="text-xs text-muted">Rationale</dt><dd className="mt-0.5">{sel.rationale}</dd></div>
          <div><dt className="text-xs text-muted">Expected effect</dt><dd className="mt-0.5">{sel.expected_effect}</dd></div>
          <div><dt className="text-xs text-muted">Rollback strategy</dt><dd className="mt-0.5">{sel.rollback_strategy}</dd></div>
          <div><dt className="text-xs text-muted">Prerequisites</dt><dd className="mt-0.5">{sel.prerequisites.join("; ") || "–"}</dd></div>
          <div className="sm:col-span-2"><dt className="text-xs text-muted">Affected components</dt><dd className="mt-0.5">{sel.affected_components.join(", ")}</dd></div>
          {sel.policy_reasons.length > 0 && (
            <div className="sm:col-span-2"><dt className="text-xs text-muted">Policy dry run at planning time (before any simulation)</dt><dd className="mt-0.5">{sel.policy_reasons.join("; ")}</dd></div>
          )}
        </dl>
      )}
      {!sel && plan.escalation_reason && <p className="text-sm text-serious">Escalation: {plan.escalation_reason}</p>}
    </div>
  );
}

// `floor`: absolute differences below this are measurement noise for a ~15s sandbox probe, not a change.
const SIM_ROWS: { key: keyof SimMetrics; label: string; fmt: (v: number) => string; better: "lower" | "higher"; floor: number }[] = [
  { key: "errorRate", label: "Error rate", fmt: (v) => pct(v), better: "lower", floor: 0.005 },
  { key: "p50Ms", label: "p50 latency", fmt: ms, better: "lower", floor: 10 },
  { key: "p95Ms", label: "p95 latency", fmt: ms, better: "lower", floor: 15 },
  { key: "p99Ms", label: "p99 latency", fmt: ms, better: "lower", floor: 25 },
  { key: "throughputRps", label: "Throughput", fmt: (v) => `${v.toFixed(1)}/s`, better: "higher", floor: 1 },
  { key: "cpuMillicores", label: "CPU", fmt: (v) => `${Math.round(v)}m`, better: "lower", floor: 25 },
  { key: "memoryGrowthMb", label: "Memory growth", fmt: (v) => `${v.toFixed(1)} MB`, better: "lower", floor: 2 },
];

export function SimulationPanel({ sims }: { sims: Simulation[] }) {
  if (!sims.length) return <Empty>No sandbox simulation was needed for the selected action.</Empty>;
  return (
    <div className="space-y-4">
      {sims.map((s) => (
        <div key={s.id}>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Mono>{s.action_type}</Mono><span className="text-ink2">on {s.target.name}</span>
            <Chip tone={s.verdict === "Improved" ? "good" : s.verdict === "Regressed" ? "critical" : "neutral"}>
              {s.verdict ?? s.phase}</Chip>
            <span className="ml-auto text-xs text-muted">{clock(s.created_at)}</span>
          </div>
          {s.baseline && s.candidate ? (
            <table className="mt-2 w-full text-sm">
              <caption className="sr-only">Sandbox baseline versus candidate under identical synthetic load</caption>
              <thead><tr className="text-left text-xs text-muted">
                <th className="py-1 font-normal">Measured in sandbox</th><th className="py-1 text-right font-normal">Current spec</th>
                <th className="py-1 text-right font-normal">With remediation</th><th className="py-1 pl-3 font-normal">Change</th></tr></thead>
              <tbody className="tabular">
                {SIM_ROWS.map((r) => {
                  const b = s.baseline![r.key] as number;
                  const c = s.candidate![r.key] as number;
                  const better = r.better === "lower" ? c < b : c > b;
                  const same = Math.abs(c - b) < r.floor || (b !== 0 && Math.abs(c - b) / Math.abs(b) < 0.1);
                  return (
                    <tr key={r.key} className="border-t border-line/60">
                      <td className="py-1 text-ink2">{r.label}</td><td className="text-right">{r.fmt(b)}</td>
                      <td className="text-right">{r.fmt(c)}</td>
                      <td className="pl-3 text-xs">{same ? <span className="text-muted">no change</span> : better
                        ? <span className="text-good"><Check size={12} className="inline" /> better</span>
                        : <span className="text-critical"><X size={12} className="inline" /> worse</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <p className="mt-1 text-sm text-ink2">{s.reason || s.summary || "No measurements."}</p>}
          {s.baseline && <p className="mt-1 text-xs text-muted">{s.baseline.requests} vs {s.candidate?.requests} probe requests; {s.summary}</p>}
        </div>
      ))}
    </div>
  );
}

const CHECK_LABEL: Record<string, string> = {
  error_ratio: "5xx error ratio", latency_p95_ms: "p95 latency", availability: "Ready replicas", saturation: "CPU of limit",
  stability: "No crash loops or OOM kills", no_active_anomalies: "No active anomalies", telemetry: "Telemetry present",
};

function checkValue(name: string, v: number | null): string {
  if (v === null || v === undefined) return "–";
  if (name === "error_ratio" || name === "saturation") return pct(v);
  if (name.startsWith("latency")) return ms(v);
  if (name === "availability") return String(Math.round(v));
  return v.toFixed(2);
}

export function ActionsPanel({ actions, verifications }: { actions: ActionView[]; verifications: Verification[] }) {
  if (!actions.length) return <Empty>No actions were submitted to the control plane.</Empty>;
  return (
    <ul className="space-y-3">
      {actions.map((a) => {
        const ver = verifications.find((v) => v.action_id === a.id);
        return (
          <li key={a.id} className="rounded border border-line p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Mono className="text-ink">{a.action_type}</Mono><span className="text-ink2">on {a.target.name}</span>
              <Phase phase={a.phase} /><Risk level={a.risk_level} />
              {a.revert_of && <Chip tone="serious">reverts {a.revert_of}</Chip>}
              <span className="ml-auto text-xs text-muted">{clock(a.created_at)}</span>
            </div>
            <p className="mt-1 text-sm text-ink2">{a.message}</p>
            {a.decision?.checks && (
              <details className="mt-2 text-sm">
                <summary className="cursor-pointer text-xs text-muted">Policy checks ({a.decision.checks.length})</summary>
                <ul className="mt-1 space-y-0.5 text-xs">
                  {a.decision.checks.map((c, i) => (
                    <li key={i} className="flex gap-2">
                      {c.passed ? <Check size={12} className="mt-0.5 text-good" aria-label="passed" /> : <X size={12} className="mt-0.5 text-critical" aria-label="failed" />}
                      <span className="font-mono">{c.name}</span><span className="text-ink2">{c.detail}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {ver && (
              <div className="mt-3">
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-muted">Verification</span>
                  <Chip tone={ver.outcome === "RESOLVED" ? "good" : ver.outcome === "PARTIAL" ? "warning" : "critical"}>{humanize(ver.outcome)}</Chip>
                </div>
                <table className="mt-1 w-full text-xs">
                  <thead><tr className="text-left text-muted"><th className="py-1 font-normal">Check</th><th className="font-normal">Service</th>
                    <th className="text-right font-normal">Before</th><th className="text-right font-normal">After</th><th className="pl-2 font-normal">Result</th></tr></thead>
                  <tbody className="tabular">
                    {ver.checks.map((c, i) => (
                      <tr key={i} className="border-t border-line/60">
                        <td className="py-0.5">{CHECK_LABEL[c.name] ?? humanize(c.name)}</td><td>{c.service}</td>
                        <td className="text-right">{checkValue(c.name, c.before)}</td>
                        <td className="text-right">{checkValue(c.name, c.after)}</td>
                        <td className="pl-2">{c.passed ? <span className="text-good">pass</span> : <span className="text-critical">fail</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ApprovalPanel({ approval, canApprove, onDone }: { approval: Approval; canApprove: boolean; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const c = approval.context.candidate;
  const decision = approval.context.decision;

  async function decide(d: "approved" | "rejected") {
    setBusy(d);
    setError(null);
    try {
      await api(`/api/v1/approvals/${approval.id}/decision`, { method: "POST", json: { decision: d, reason } });
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3 text-sm">
      <p className="text-ink">
        AegisOps wants to run <Mono className="text-ink">{c?.action_type}</Mono> on <strong>{c?.target.name}</strong>
        {c && paramText(c.params) !== "–" ? <> with <Mono>{paramText(c.params)}</Mono></> : null}.
      </p>
      <dl className="grid grid-cols-2 gap-2">
        <div><dt className="text-xs text-muted">Risk</dt><dd><Risk level={decision?.riskLevel ?? c?.risk_level} /></dd></div>
        <div><dt className="text-xs text-muted">Diagnosis confidence</dt><dd>{pct(approval.context.confidence, 0)}</dd></div>
        <div className="col-span-2"><dt className="text-xs text-muted">Why approval is required</dt>
          <dd className="text-ink2">{decision?.reasons?.join("; ") ?? "policy"}</dd></div>
        <div className="col-span-2"><dt className="text-xs text-muted">Expected effect</dt><dd className="text-ink2">{c?.expected_effect}</dd></div>
        <div className="col-span-2"><dt className="text-xs text-muted">Rollback</dt><dd className="text-ink2">{c?.rollback_strategy}</dd></div>
        <div className="col-span-2"><dt className="text-xs text-muted">Affected</dt><dd className="text-ink2">{c?.affected_components.join(", ")}</dd></div>
        <div className="col-span-2"><dt className="text-xs text-muted">Sandbox simulation</dt>
          <dd className="text-ink2">{approval.context.simulation ?? "not available for this action; decision rests on policy"}</dd></div>
      </dl>
      {canApprove ? (
        <>
          <label className="block">
            <span className="text-xs text-muted">Reason (recorded in the audit trail)</span>
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={500}
              className="mt-1 w-full rounded border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-accent" />
          </label>
          {error ? <ErrorNote error={error} /> : null}
          <div className="flex gap-2">
            <Button variant="primary" busy={busy === "approved"} disabled={!!busy} onClick={() => decide("approved")}>Approve and run</Button>
            <Button variant="danger" busy={busy === "rejected"} disabled={!!busy} onClick={() => decide("rejected")}>Reject</Button>
          </div>
        </>
      ) : (
        <p className="text-xs text-muted">Your role cannot approve actions. An approver or admin must decide.</p>
      )}
    </div>
  );
}
