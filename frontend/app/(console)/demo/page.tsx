"use client";

import { FlaskConical, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { Button, Chip, Empty, ErrorNote, Loading, Mono, Panel } from "@/components/ui";
import { api, can, fetcher } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { IncidentSummary } from "@/lib/types";

type Scenario = { id: string; title: string; fault_class: string; description: string; fault_type: string; target: string;
  demo: boolean; tags: string[] };
type Status = { namespace: string; drift: string[]; toxics: string[]; traffic: Record<string, unknown>;
  canaries: { name: string; phase: string }[] };

export default function DemoPage() {
  const { data, error } = useSWR<{ items: Scenario[] }>("/api/v1/demo/scenarios", fetcher);
  const { data: status, mutate: refreshStatus } = useSWR<Status>("/api/v1/demo/status", fetcher, { refreshInterval: 10000 });
  const { data: active } = useSWR<{ items: IncidentSummary[] }>("/api/v1/incidents?status=active&limit=5", fetcher);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [showAll, setShowAll] = useState(false);

  async function run(label: string, path: string) {
    setBusy(label);
    setErr(null);
    setMsg(null);
    try {
      const res = await api<Record<string, unknown>>(path, { method: "POST" });
      setMsg(label === "reset" ? `Environment reset: ${JSON.stringify(res.restored ?? [])}` : `Injected ${label}: ${String(res.applied ?? "ok")}`);
      await refreshStatus();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading scenarios" />;
  const scenarios = showAll ? data.items : data.items.filter((s) => s.demo);
  const dirty = status && (status.drift.length || status.toxics.length || status.canaries.length);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-xl font-semibold">Demo scenarios</h1>
          <p className="mt-1 max-w-3xl text-ink2">
            Controlled faults for the ShopFlow demo namespace only. The injector refuses any other namespace and uses the same
            APIs a release engineer would; AegisOps sees only telemetry, never the scenario definition.
          </p>
        </div>
        {can("demo") && (
          <Button onClick={() => run("reset", "/api/v1/demo/reset")} busy={busy === "reset"}>
            <RotateCcw size={14} aria-hidden /> Reset environment</Button>
        )}
      </div>

      {msg && <p role="status" className="rounded border border-accent/40 bg-accent/10 px-3 py-2 text-sm">{msg}</p>}
      {err ? <ErrorNote error={err} /> : null}

      <Panel title="Environment state">
        {!status ? <Loading /> : (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {dirty ? <Chip tone="warning">faults present</Chip> : <Chip tone="good">matches baseline</Chip>}
            {status.drift.length > 0 && <span className="text-ink2">drifted: {status.drift.join(", ")}</span>}
            {status.toxics.length > 0 && <span className="text-ink2">network faults: {status.toxics.join(", ")}</span>}
            {status.canaries.length > 0 && <span className="text-ink2">canaries: {status.canaries.map((c) => `${c.name} (${c.phase})`).join(", ")}</span>}
            {active?.items.length ? (
              <span className="text-ink2">active incident: {active.items.map((i) => (
                <Link key={i.id} href={`/incidents/${i.id}`} className="ml-1 text-accent hover:underline">{i.id}</Link>))}</span>
            ) : null}
          </div>
        )}
      </Panel>

      <div className="flex items-center justify-between">
        <h2 className="font-display text-md font-semibold">{showAll ? "All benchmark scenarios" : "Recommended for a live demo"}</h2>
        <button onClick={() => setShowAll(!showAll)} className="text-sm text-accent hover:underline">
          {showAll ? "Show demo set" : `Show all ${data.items.length}`}</button>
      </div>
      {scenarios.length === 0 ? <Empty>No scenarios.</Empty> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {scenarios.map((s) => (
            <div key={s.id} className="flex flex-col rounded-lg border border-line bg-panel p-4">
              <div className="flex items-center gap-2">
                <Chip icon={false}>{humanize(s.fault_class)}</Chip>
                <Mono className="text-muted">{s.fault_type}</Mono>
              </div>
              <h3 className="mt-2 font-display text-md font-semibold">{s.title}</h3>
              <p className="mt-1 flex-1 text-sm text-ink2">{s.description}</p>
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-muted">target {s.target}</span>
                {can("demo") && (
                  <Button variant="primary" busy={busy === s.id} disabled={!!busy} onClick={() => run(s.id, `/api/v1/demo/scenarios/${s.id}/inject`)}>
                    <FlaskConical size={14} aria-hidden /> Inject</Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
