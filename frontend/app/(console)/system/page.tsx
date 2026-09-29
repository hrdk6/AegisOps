"use client";

import useSWR from "swr";
import { Chip, Empty, ErrorNote, Loading, Mono, Panel } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, clock } from "@/lib/format";

type Component = { name: string; status: string; latency_ms?: number; detail: string | null; details?: Record<string, unknown> };
type Health = { status: string; components: Component[]; checked_at: string };
type Usage = {
  summary: { provider: string; model: string; status: string; calls: number; prompt_tokens: number; completion_tokens: number;
    cost_usd: number; avg_latency_ms: number }[];
  recent: { id: number; incident_id: string | null; purpose: string; provider: string; model: string; status: string; latency_ms: number;
    prompt_tokens: number; completion_tokens: number; cost_usd: number; attempt: number; fallback: boolean; error: string | null;
    created_at: string }[];
};

const TONE: Record<string, "good" | "warning" | "critical" | "neutral"> = { ok: "good", idle: "neutral", degraded: "warning", down: "critical" };

const ROLE: Record<string, string> = {
  api: "REST API, auth, SSE", database: "Incidents, evidence, audit (PostgreSQL)", controller: "Go control plane: policy and execution",
  prometheus: "Metrics", loki: "Logs", jaeger: "Traces", "chaos-injector": "Demo fault injection", engine: "Detection and incident workflow",
  "ai-layer": "Model router (optional)", "execution-audit-sync": "Controller audit ingestion",
};

export default function SystemPage() {
  const { data, error } = useSWR<Health>("/api/v1/system/health", fetcher, { refreshInterval: 15000 });
  const { data: ai } = useSWR<Usage>("/api/v1/system/ai", fetcher, { refreshInterval: 60000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Probing components" />;
  const engine = data.components.find((c) => c.name === "engine")?.details as
    { degraded_sources?: string[]; open_incidents?: number; active_workflows?: number; last_cycle?: string } | undefined;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-xl font-semibold">System health</h1>
          <p className="mt-1 text-ink2">AegisOps&apos; own components. Missing telemetry sources degrade diagnosis instead of stopping it.</p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <Chip tone={TONE[data.status] ?? "neutral"}>{data.status}</Chip><span className="text-muted">checked {clock(data.checked_at)}</span>
        </div>
      </div>

      <Panel dense>
        <table className="w-full text-sm">
          <thead><tr className="border-b border-line text-left text-xs text-muted">
            <th className="px-4 py-2 font-normal">Component</th><th className="px-2 font-normal">Role</th>
            <th className="px-2 font-normal">Status</th><th className="px-2 text-right font-normal">Probe</th>
            <th className="px-4 font-normal">Detail</th></tr></thead>
          <tbody>
            {data.components.map((c) => (
              <tr key={c.name} className="border-b border-line/60">
                <td className="px-4 py-2 font-medium">{c.name}</td>
                <td className="px-2 text-ink2">{ROLE[c.name] ?? ""}</td>
                <td className="px-2"><Chip tone={TONE[c.status] ?? "neutral"}>{c.status}</Chip></td>
                <td className="tabular px-2 text-right text-ink2">{c.latency_ms !== undefined ? `${c.latency_ms}ms` : "–"}</td>
                <td className="px-4 text-xs text-ink2">{c.detail ?? "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {engine && (
        <Panel title="Detection engine">
          <dl className="grid grid-cols-2 gap-4 md:grid-cols-4 text-sm">
            <div><dt className="text-xs text-muted">Last detection cycle</dt><dd>{ago(engine.last_cycle)}</dd></div>
            <div><dt className="text-xs text-muted">Open incidents</dt><dd>{engine.open_incidents ?? 0}</dd></div>
            <div><dt className="text-xs text-muted">Active workflows</dt><dd>{engine.active_workflows ?? 0}</dd></div>
            <div><dt className="text-xs text-muted">Degraded telemetry sources</dt>
              <dd>{engine.degraded_sources?.length ? engine.degraded_sources.join(", ") : "none"}</dd></div>
          </dl>
        </Panel>
      )}

      <Panel title="Model usage" dense>
        {!ai ? <Loading /> : ai.summary.length === 0 ? (
          <Empty>No model calls recorded. With no provider configured, diagnosis and planning run on deterministic rules only.</Empty>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-2 font-normal">Provider</th><th className="px-2 font-normal">Model</th><th className="px-2 font-normal">Status</th>
              <th className="px-2 text-right font-normal">Calls</th><th className="px-2 text-right font-normal">Tokens in / out</th>
              <th className="px-2 text-right font-normal">Cost</th><th className="px-4 text-right font-normal">Avg latency</th></tr></thead>
            <tbody className="tabular">
              {ai.summary.map((r) => (
                <tr key={`${r.provider}-${r.model}-${r.status}`} className="border-b border-line/60">
                  <td className="px-4 py-1.5">{r.provider}</td><td className="px-2"><Mono>{r.model}</Mono></td>
                  <td className="px-2"><Chip tone={r.status === "ok" ? "good" : "critical"}>{r.status}</Chip></td>
                  <td className="px-2 text-right">{r.calls}</td><td className="px-2 text-right">{r.prompt_tokens} / {r.completion_tokens}</td>
                  <td className="px-2 text-right">${r.cost_usd.toFixed(4)}</td><td className="px-4 text-right">{Math.round(r.avg_latency_ms)}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
