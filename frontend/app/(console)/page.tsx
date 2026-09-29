"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { DependencyGraph } from "@/components/DependencyGraph";
import { IncidentRow } from "@/components/IncidentRow";
import { Empty, ErrorNote, IncidentStatus, Loading, Mono, Panel, Phase, Risk, ServiceStatus } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, duration, ms, pct } from "@/lib/format";
import type { ActionView, IncidentSummary, Signals, Topology } from "@/lib/types";

type Overview = {
  services: { name: string; status: string; signals: Signals }[];
  slo_compliance: number | null;
  active_incidents: IncidentSummary[];
  recent_incidents: IncidentSummary[];
  recent_actions: ActionView[];
  risk_events: ActionView[];
  pending_approvals: number;
  automation: { mode: string; breaker?: { state?: string; recentFailures?: number } };
  last_24h: { incidents_by_outcome: Record<string, number>; mttr_seconds: number | null };
};

export default function OverviewPage() {
  const router = useRouter();
  const { data, error } = useSWR<Overview>("/api/v1/overview", fetcher, { refreshInterval: 15000 });
  const { data: topo } = useSWR<Topology>("/api/v1/topology", fetcher, { refreshInterval: 20000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading system overview" />;

  const unhealthy = data.services.filter((s) => s.status !== "healthy");
  const outcomes = data.last_24h.incidents_by_outcome;
  const resolvedAuto = (outcomes.resolved_autonomously ?? 0) + (outcomes.resolved_with_approval ?? 0);
  const total24 = Object.values(outcomes).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold">
            {unhealthy.length === 0 ? "All services within SLO" : `${unhealthy.length} service${unhealthy.length > 1 ? "s" : ""} outside SLO`}
          </h1>
          <p className="mt-1 text-ink2">
            {data.active_incidents.length
              ? `${data.active_incidents.length} active incident${data.active_incidents.length > 1 ? "s" : ""}; AegisOps is ${data.automation.mode === "autonomous" ? "remediating within policy" : `in ${data.automation.mode} mode`}.`
              : "No active incidents. The detector evaluates every service every 5 seconds."}
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
          <Stat label="SLO compliance" value={data.slo_compliance === null ? "–" : pct(data.slo_compliance, 0)} />
          <Stat label="Incidents, 24h" value={String(total24)} />
          <Stat label="Resolved by AegisOps, 24h" value={`${resolvedAuto}/${total24}`} />
          <Stat label="Mean time to recover, 24h" value={duration(data.last_24h.mttr_seconds)} />
        </dl>
      </div>

      <div className="grid gap-5 xl:grid-cols-5">
        <Panel title="Live dependency map" className="min-w-0 xl:col-span-3"
          action={<Link href="/services" className="text-sm text-accent hover:underline">All services</Link>}>
          {topo ? (
            <DependencyGraph nodes={topo.nodes} edges={topo.edges} root={topo.rootCandidates?.[0]}
              onSelect={(n) => router.push(`/services/${n}`)} height={320} />
          ) : <Loading label="Building dependency graph" />}
        </Panel>
        <Panel title="Active incidents" className="min-w-0 xl:col-span-2" dense
          action={<Link href="/incidents" className="text-sm text-accent hover:underline">History</Link>}>
          {data.active_incidents.length === 0 ? (
            <div className="p-4">
              <Empty>Nothing needs attention. Inject a fault from Demo scenarios to watch the loop run.</Empty>
              <RecentIncidents items={data.recent_incidents.slice(0, 4)} />
            </div>
          ) : (
            <ul className="divide-y divide-line">{data.active_incidents.map((i) => <IncidentRow key={i.id} incident={i} />)}</ul>
          )}
        </Panel>
      </div>

      <Panel title="Services" dense>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-2 font-normal">Service</th><th className="px-2 font-normal">Status</th>
                <th className="px-2 text-right font-normal">Requests/s</th><th className="px-2 text-right font-normal">5xx</th>
                <th className="px-2 text-right font-normal">p95</th><th className="px-2 text-right font-normal">CPU of limit</th>
                <th className="px-2 text-right font-normal">Memory of limit</th><th className="px-2 text-right font-normal">Ready</th>
                <th className="px-4 font-normal">Anomalies</th>
              </tr>
            </thead>
            <tbody className="tabular">
              {data.services.map(({ name, status, signals: s }) => (
                <tr key={name} className="border-b border-line/60 hover:bg-raised/50">
                  <td className="px-4 py-2"><Link href={`/services/${name}`} className="hover:text-accent">{name}</Link></td>
                  <td className="px-2"><ServiceStatus status={status} /></td>
                  <td className="px-2 text-right">{s.rps?.toFixed(2) ?? "–"}</td>
                  <td className="px-2 text-right">{pct(s.error_ratio)}</td>
                  <td className="px-2 text-right">{ms(s.p95_ms)}</td>
                  <td className="px-2 text-right">{pct(s.cpu_util, 0)}</td>
                  <td className="px-2 text-right">{pct(s.mem_util, 0)}</td>
                  <td className="px-2 text-right">{s.ready}/{s.desired}</td>
                  <td className="px-4 text-xs text-ink2">{s.anomalies.map((a) => a.reason).join("; ") || "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Recent actions" dense>
          <ActionList items={data.recent_actions} empty="No remediation actions yet." />
        </Panel>
        <Panel title="Risk events" dense action={<Link href="/automation" className="text-sm text-accent hover:underline">Policy</Link>}>
          <ActionList items={data.risk_events} empty="No denied or approval-gated actions." showReasons />
        </Panel>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="tabular font-display text-lg font-semibold">{value}</dd>
    </div>
  );
}

function RecentIncidents({ items }: { items: IncidentSummary[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-2">
      <h3 className="mb-1 text-xs text-muted">Most recent</h3>
      <ul className="space-y-1.5">
        {items.map((i) => (
          <li key={i.id} className="flex items-center justify-between gap-2 text-sm">
            <Link href={`/incidents/${i.id}`} className="truncate hover:text-accent">{i.title}</Link>
            <IncidentStatus status={i.status} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function ActionList({ items, empty, showReasons }: { items: ActionView[]; empty: string; showReasons?: boolean }) {
  if (!items.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="divide-y divide-line">
      {items.map((a) => (
        <li key={a.id} className="px-4 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Mono className="text-ink">{a.action_type}</Mono>
            <span className="text-ink2">on {a.target.name}</span>
            <Phase phase={a.phase} />
            <Risk level={a.risk_level} />
            <span className="ml-auto text-xs text-muted">{ago(a.created_at)}</span>
          </div>
          <div className="mt-0.5 text-xs text-muted">
            <Link href={`/incidents/${a.incident_id}`} className="hover:text-accent">{a.incident_id}</Link>
            {showReasons && a.decision?.reasons?.length ? ` · ${a.decision.reasons.slice(0, 2).join("; ")}` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}
