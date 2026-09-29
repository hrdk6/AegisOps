"use client";

import clsx from "clsx";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { TimeSeries } from "@/components/charts";
import { Chip, Empty, ErrorNote, Field, Loading, Mono, Panel, ServiceStatus } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, ms, pct } from "@/lib/format";
import type { Signals } from "@/lib/types";

type Workload = {
  name: string; tier?: string; owner?: string; replicas: number; readyReplicas: number; revision: number; rolloutComplete: boolean;
  rolloutMessage?: string; dependencies: string[]; dependents: string[]; simulationEnabled?: boolean;
  labels?: Record<string, string>;
  containers: { name: string; image: string; requests?: Record<string, string>; limits?: Record<string, string>;
    configMaps?: string[] }[];
  revisions: { revision: number; replicaSet: string; createdAt: string; images: string[]; replicas: number; version?: string }[];
  pods?: { name: string; phase: string; ready: boolean; restarts: number; waitingReason?: string; lastTerminationReason?: string;
    podTemplateHash?: string }[];
};
type Change = { id: string; time: string; kind: string; name: string; type: string; summary: string; actor?: string;
  fields: { path: string; old?: string; new?: string }[] };
type Detail = { name: string; status: string; signals: Partial<Signals>; workload: Workload; changes: Change[] };
type Metrics = { series: Record<string, [number, number][]> };

const WINDOWS = [15, 30, 60, 180];

export default function ServicePage() {
  const { name } = useParams<{ name: string }>();
  const [minutes, setMinutes] = useState(30);
  const { data, error } = useSWR<Detail>(`/api/v1/services/${name}`, fetcher, { refreshInterval: 15000 });
  const { data: m, error: mErr } = useSWR<Metrics>(`/api/v1/services/${name}/metrics?minutes=${minutes}`, fetcher,
    { refreshInterval: 20000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label={`Loading ${name}`} />;
  const w = data.workload;
  const s = data.signals;
  const isHttp = s.rps !== null && s.rps !== undefined;
  const version = w.revisions.find((r) => r.revision === w.revision)?.version ?? w.labels?.version;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/services" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft size={14} aria-hidden /> Services
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="font-display text-xl font-semibold">{name}</h1>
          <ServiceStatus status={data.status} />
          {w.tier && <Chip icon={false}>{w.tier}</Chip>}
          {!w.rolloutComplete && <Chip tone="warning">{w.rolloutMessage ?? "rolling out"}</Chip>}
        </div>
        {s.anomalies?.length ? (
          <ul className="mt-2 space-y-0.5 text-sm text-serious">{s.anomalies.map((a) => <li key={a.signal}>{a.reason}</li>)}</ul>
        ) : null}
      </div>

      <Panel>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
          <Field label="Version"><Mono>{version ?? "–"}</Mono> <span className="text-xs text-muted">rev {w.revision}</span></Field>
          <Field label="Ready replicas">{w.readyReplicas}/{w.replicas}</Field>
          <Field label="Owner">{w.owner ?? "–"}</Field>
          <Field label="Depends on">{w.dependencies.join(", ") || "–"}</Field>
          <Field label="Used by">{w.dependents.join(", ") || "–"}</Field>
          <Field label="Sandbox simulation">{w.simulationEnabled ? "enabled" : "not configured"}</Field>
          <Field label="Signals updated">{ago(s.at)}</Field>
        </dl>
      </Panel>

      <Panel title="Telemetry" action={
        <div role="radiogroup" aria-label="Time window" className="flex rounded border border-line p-0.5 text-xs">
          {WINDOWS.map((x) => (
            <button key={x} role="radio" aria-checked={minutes === x} onClick={() => setMinutes(x)}
              className={clsx("rounded-sm px-2 py-0.5", minutes === x ? "bg-raised text-ink" : "text-muted hover:text-ink2")}>
              {x < 60 ? `${x}m` : `${x / 60}h`}
            </button>
          ))}
        </div>}>
        {mErr ? <ErrorNote error={mErr} /> : !m ? <Loading label="Querying Prometheus" /> : (
          <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
            {isHttp && <TimeSeries title="Requests per second" points={m.series.rps ?? []} format={(v) => v.toFixed(1)} />}
            {isHttp && <TimeSeries title="5xx error ratio" points={m.series.error_ratio ?? []} format={(v) => pct(v)} threshold={0.02}
              thresholdLabel="SLO 2%" color="var(--series-2)" />}
            {isHttp && <TimeSeries title="p95 latency" points={m.series.p95_ms ?? []} format={ms} color="var(--series-3)" />}
            <TimeSeries title="CPU (cores)" points={m.series.cpu_cores ?? []} format={(v) => v.toFixed(2)} />
            <TimeSeries title="Memory working set" points={m.series.memory_mb ?? []} format={(v) => `${Math.round(v)} MB`} />
            {(m.series.db_pool_in_use ?? []).length > 0 && (
              <TimeSeries title="DB connections in use" points={m.series.db_pool_in_use} format={(v) => v.toFixed(0)} />
            )}
          </div>
        )}
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Rollout history" dense>
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-2 font-normal">Revision</th><th className="px-2 font-normal">Version</th>
              <th className="px-2 font-normal">Image</th><th className="px-2 text-right font-normal">Pods</th>
              <th className="px-4 text-right font-normal">Created</th></tr></thead>
            <tbody>
              {w.revisions.map((r) => (
                <tr key={r.revision} className={clsx("border-b border-line/60", r.revision === w.revision && "bg-accent/5")}>
                  <td className="px-4 py-1.5">{r.revision}{r.revision === w.revision && <span className="ml-1 text-xs text-accent">current</span>}</td>
                  <td className="px-2"><Mono>{r.version ?? "–"}</Mono></td>
                  <td className="px-2"><Mono className="text-ink2">{r.images.map((i) => i.split("/").pop()).join(", ")}</Mono></td>
                  <td className="tabular px-2 text-right">{r.replicas}</td>
                  <td className="px-4 text-right text-ink2">{ago(r.createdAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Pods" dense>
          {!w.pods?.length ? <Empty>No pods.</Empty> : (
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-2 font-normal">Pod</th><th className="px-2 font-normal">State</th>
                <th className="px-4 text-right font-normal">Restarts</th></tr></thead>
              <tbody>
                {w.pods.map((p) => (
                  <tr key={p.name} className="border-b border-line/60">
                    <td className="px-4 py-1.5"><Mono>{p.name}</Mono></td>
                    <td className="px-2">
                      <Chip tone={p.ready ? "good" : p.waitingReason ? "critical" : "warning"}>{p.waitingReason ?? (p.ready ? "ready" : p.phase)}</Chip>
                      {p.lastTerminationReason && <span className="ml-1 text-xs text-muted">last exit: {p.lastTerminationReason}</span>}
                    </td>
                    <td className="tabular px-4 text-right">{p.restarts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      <Panel title="Recent changes (6h)" dense>
        {!data.changes.length ? <Empty>No recorded changes to this service.</Empty> : (
          <ul className="divide-y divide-line">
            {data.changes.map((c) => (
              <li key={c.id} className="px-4 py-2.5">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Chip icon={false}>{c.type}</Chip><span className="text-ink">{c.kind} {c.name}</span>
                  {c.actor && <span className="text-xs text-muted">by {c.actor}</span>}
                  <span className="ml-auto text-xs text-muted">{ago(c.time)}</span>
                </div>
                <ul className="mt-1 space-y-0.5 text-xs text-ink2">
                  {c.fields.slice(0, 6).map((f, i) => (
                    <li key={i}><Mono>{f.path}</Mono>: {f.old || "<none>"} → {f.new || "<none>"}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
