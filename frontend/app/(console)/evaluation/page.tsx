"use client";

import Link from "next/link";
import useSWR from "swr";
import { Chip, Empty, ErrorNote, Loading, Mono, Panel } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { fmtDur, type Run } from "@/lib/evaluation";
import { ago, pct } from "@/lib/format";

export default function EvaluationPage() {
  const { data, error } = useSWR<{ items: Run[] }>("/api/v1/evaluations", fetcher, { refreshInterval: 30000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading benchmark runs" />;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="font-display text-xl font-semibold">Evaluation</h1>
        <p className="mt-1 max-w-3xl text-ink2">
          Benchmark runs inject controlled faults into the demo environment and score what AegisOps did against each
          scenario&apos;s ground truth, which the engine never sees. Rates carry 95% Wilson intervals; with few runs they are wide.
        </p>
      </div>
      <Panel dense>
        {data.items.length === 0 ? (
          <Empty>No benchmark runs yet. Run <Mono>aegis benchmark run</Mono> to create one.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-2 font-normal">Run</th><th className="px-2 font-normal">Mode</th>
                <th className="px-2 font-normal">Status</th><th className="px-2 text-right font-normal">Scenarios × reps</th>
                <th className="px-2 text-right font-normal">Pass rate</th><th className="px-2 text-right font-normal">Root cause top-1</th>
                <th className="px-2 text-right font-normal">Unsafe actions</th><th className="px-2 font-normal">Recovery time</th>
                <th className="px-4 text-right font-normal">Started</th></tr></thead>
              <tbody>
                {data.items.map((r) => {
                  const s = r.summary && "runs" in r.summary ? r.summary : null;
                  return (
                    <tr key={r.id} className="border-b border-line/60 hover:bg-raised/50">
                      <td className="px-4 py-2"><Link href={`/evaluation/${r.id}`} className="font-mono text-[12.5px] hover:text-accent">{r.id}</Link></td>
                      <td className="px-2"><Chip icon={false} tone={r.mode === "live" ? "accent" : "neutral"}>{r.mode}</Chip></td>
                      <td className="px-2"><Chip tone={r.status === "completed" ? "good" : r.status === "running" ? "warning" : "critical"}>{r.status}</Chip></td>
                      <td className="tabular px-2 text-right">{s ? `${s.scenarios} / ${s.runs}` : "–"}</td>
                      <td className="tabular px-2 text-right">{s ? `${pct(s.pass_rate.value, 0)} (${s.pass_rate.k}/${s.pass_rate.n})` : "–"}</td>
                      <td className="tabular px-2 text-right">{s ? pct(s.root_cause_accuracy.value, 0) : "–"}</td>
                      <td className="tabular px-2 text-right">{s ? `${s.unsafe_action_rate.k}/${s.unsafe_action_rate.n}` : "–"}</td>
                      <td className="px-2 text-xs text-ink2">{s ? fmtDur(s.recovery_time_s) : "–"}</td>
                      <td className="px-4 text-right text-ink2">{ago(r.started_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
