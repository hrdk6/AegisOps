"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { RateIntervals } from "@/components/charts";
import { Chip, ErrorNote, Field, Loading, Mono, Panel } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { fmtDur, rateRows, type Run } from "@/lib/evaluation";
import { clock, humanize } from "@/lib/format";

type Result = {
  scenario_id: string; repetition: number; incident_id: string | null; passed: boolean;
  metrics: {
    fault_class: string; detected: boolean; time_to_detect_s: number | null; root_cause_correct: boolean | null;
    actions_executed: string[]; outcome: string; outcome_expected: string[]; recovery_time_s: number | null;
    diagnosis_confidence: number | null; unsafe_action: boolean; false_remediation: boolean; notes: string[];
  };
};

export default function EvaluationRunPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error } = useSWR<Run & { results: Result[] }>(`/api/v1/evaluations/${id}`, fetcher, { refreshInterval: 30000 });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading run" />;
  const s = data.summary && "runs" in data.summary ? data.summary : null;
  const meta = data.metadata as Record<string, string | number | boolean | Record<string, unknown>>;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/evaluation" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft size={14} aria-hidden /> Evaluation
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <h1 className="font-mono text-lg font-semibold">{data.id}</h1>
          <Chip tone={data.mode === "live" ? "accent" : "neutral"} icon={false}>{data.mode}</Chip>
          <Chip tone={data.status === "completed" ? "good" : data.status === "running" ? "warning" : "critical"}>{data.status}</Chip>
        </div>
      </div>

      <Panel title="Reproducibility">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Git commit"><Mono>{String(meta.git_sha ?? "–")}{meta.git_dirty ? " (dirty)" : ""}</Mono></Field>
          <Field label="Source hash"><Mono>{String(meta.source_hash ?? "–")}</Mono></Field>
          <Field label="Policy hash"><Mono>{String(meta.policy_hash ?? "–")}</Mono></Field>
          <Field label="Config hash"><Mono>{String(meta.config_hash ?? "–")}</Mono></Field>
          <Field label="Repetitions">{String(meta.repetitions ?? "–")}</Field>
          <Field label="Model routes">{typeof meta.model_routes === "object" ? JSON.stringify(meta.model_routes) : String(meta.model_routes ?? "–")}</Field>
          <Field label="Started">{clock(data.started_at)}</Field>
          <Field label="Finished">{clock(data.finished_at)}</Field>
        </dl>
      </Panel>

      {s ? (
        <div className="grid gap-5 xl:grid-cols-5">
          <Panel title="Rates with 95% confidence intervals" className="min-w-0 xl:col-span-3">
            <div className="overflow-x-auto"><RateIntervals rows={rateRows(s)} /></div>
          </Panel>
          <Panel title="Timing and effort" className="min-w-0 xl:col-span-2">
            <dl className="space-y-2.5 text-sm">
              <div><dt className="text-xs text-muted">Time to detect (injection → incident)</dt><dd>{fmtDur(s.time_to_detect_s)}</dd></div>
              <div><dt className="text-xs text-muted">Detection latency (anomaly onset → incident)</dt><dd>{fmtDur(s.detection_latency_s)}</dd></div>
              <div><dt className="text-xs text-muted">Recovery time (injection → verified resolution)</dt><dd>{fmtDur(s.recovery_time_s)}</dd></div>
              <div><dt className="text-xs text-muted">MTTR (detection → verified resolution)</dt><dd>{fmtDur(s.mttr_s)}</dd></div>
              <div><dt className="text-xs text-muted">Actions per detected incident</dt><dd>{fmtDur(s.actions_per_incident).replace(/s\b/g, "")}</dd></div>
              <div><dt className="text-xs text-muted">Confidence calibration (Brier, lower is better)</dt><dd>{s.calibration_brier ?? "n/a"}</dd></div>
            </dl>
          </Panel>
        </div>
      ) : <Panel><p className="text-sm text-ink2">This run has no summary yet.</p></Panel>}

      <Panel title="Per scenario" dense>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-2 font-normal">Scenario</th><th className="px-2 font-normal">Class</th>
              <th className="px-2 text-right font-normal">Detect</th><th className="px-2 font-normal">Root cause</th>
              <th className="px-2 font-normal">Actions executed</th><th className="px-2 font-normal">Outcome (expected)</th>
              <th className="px-2 text-right font-normal">Recovery</th><th className="px-4 font-normal">Result</th></tr></thead>
            <tbody>
              {data.results.map((r) => {
                const m = r.metrics;
                return (
                  <tr key={`${r.scenario_id}-${r.repetition}`} className="border-b border-line/60 align-top">
                    <td className="px-4 py-2"><Mono>{r.scenario_id}</Mono>{r.repetition > 1 && <span className="text-xs text-muted"> #{r.repetition}</span>}
                      {r.incident_id && <Link href={`/incidents/${r.incident_id}`} className="block text-xs text-muted hover:text-accent">{r.incident_id}</Link>}</td>
                    <td className="px-2 text-xs text-ink2">{humanize(m.fault_class)}</td>
                    <td className="tabular px-2 text-right">{m.detected ? `${m.time_to_detect_s ?? "–"}s` : "no"}</td>
                    <td className="px-2">{m.root_cause_correct === null ? <span className="text-muted">n/a</span>
                      : <Chip tone={m.root_cause_correct ? "good" : "critical"}>{m.root_cause_correct ? "correct" : "wrong"}</Chip>}</td>
                    <td className="px-2 text-xs"><Mono>{m.actions_executed.join(", ") || "–"}</Mono>
                      {m.unsafe_action && <Chip tone="critical" className="ml-1">unsafe</Chip>}</td>
                    <td className="px-2 text-xs">{humanize(m.outcome)} <span className="text-muted">({m.outcome_expected.map(humanize).join(" / ")})</span></td>
                    <td className="tabular px-2 text-right">{m.recovery_time_s !== null ? `${m.recovery_time_s}s` : "–"}</td>
                    <td className="px-4"><Chip tone={r.passed ? "good" : "critical"}>{r.passed ? "pass" : "fail"}</Chip>
                      {!r.passed && m.notes?.length ? <p className="mt-1 max-w-xs text-xs text-muted">{m.notes.join("; ")}</p> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
