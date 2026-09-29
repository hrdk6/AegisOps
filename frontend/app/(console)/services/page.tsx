"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { DependencyGraph } from "@/components/DependencyGraph";
import { Chip, ErrorNote, Loading, Mono, Panel, ServiceStatus } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ms, pct } from "@/lib/format";
import type { Signals, Topology } from "@/lib/types";

type Service = {
  name: string; status: string; signals: Partial<Signals>; tier: string | null; replicas: number | null; ready: number | null;
  revision: number | null; version: string | null; owner: string | null; dependencies: string[]; dependents: string[];
  rollout_complete: boolean | null;
};

const TIER_TONE = { critical: "serious", stateful: "warning", infrastructure: "warning" } as const;

export default function ServicesPage() {
  const router = useRouter();
  const { data, error } = useSWR<{ items: Service[]; controlplane_available: boolean }>("/api/v1/services", fetcher,
    { refreshInterval: 15000 });
  const { data: topo } = useSWR<Topology>("/api/v1/topology", fetcher, { refreshInterval: 30000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading services" />;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="font-display text-xl font-semibold">Services</h1>
        <p className="mt-1 text-ink2">
          ShopFlow workloads in the managed namespace. Edges combine declared dependencies, client-side metrics and traces.
        </p>
        {!data.controlplane_available && <p className="mt-2 text-sm text-serious">Control plane unreachable: rollout details are missing.</p>}
      </div>
      <Panel title="Dependency graph">
        {topo ? <DependencyGraph nodes={topo.nodes} edges={topo.edges} root={topo.rootCandidates?.[0]}
          onSelect={(n) => router.push(`/services/${n}`)} height={360} /> : <Loading label="Building graph" />}
        <p className="mt-2 text-xs text-muted">Solid edges were observed in traffic (metrics or traces); dashed edges are declared only.</p>
      </Panel>
      <Panel dense>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-2 font-normal">Service</th><th className="px-2 font-normal">Status</th>
                <th className="px-2 font-normal">Tier</th><th className="px-2 font-normal">Version</th>
                <th className="px-2 text-right font-normal">Ready</th><th className="px-2 text-right font-normal">5xx</th>
                <th className="px-2 text-right font-normal">p95</th><th className="px-2 font-normal">Depends on</th>
                <th className="px-4 font-normal">Owner</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={s.name} className="border-b border-line/60 hover:bg-raised/50">
                  <td className="px-4 py-2"><Link href={`/services/${s.name}`} className="font-medium hover:text-accent">{s.name}</Link></td>
                  <td className="px-2"><ServiceStatus status={s.status} /></td>
                  <td className="px-2">{s.tier ? <Chip tone={TIER_TONE[s.tier as keyof typeof TIER_TONE] ?? "neutral"} icon={false}>{s.tier}</Chip> : "–"}</td>
                  <td className="px-2"><Mono>{s.version ?? "–"}</Mono>{s.revision ? <span className="text-xs text-muted"> rev {s.revision}</span> : null}
                    {s.rollout_complete === false && <Chip tone="warning" className="ml-1">rolling out</Chip>}</td>
                  <td className="tabular px-2 text-right">{s.ready ?? "–"}/{s.replicas ?? "–"}</td>
                  <td className="tabular px-2 text-right">{pct(s.signals.error_ratio)}</td>
                  <td className="tabular px-2 text-right">{ms(s.signals.p95_ms)}</td>
                  <td className="px-2 text-xs text-ink2">{s.dependencies.join(", ") || "–"}</td>
                  <td className="px-4 text-xs text-ink2">{s.owner ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
