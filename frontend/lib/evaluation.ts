import type { RateRow } from "@/components/charts";

export type Rate = { value: number | null; k: number; n: number; ci95: [number, number] };
export type Dur = { mean: number | null; median: number | null; p90: number | null; n: number; ci95: [number, number] | null };

export type Summary = {
  runs: number; scenarios: number; pass_rate: Rate; detection_rate: Rate; root_cause_accuracy: Rate; root_cause_top3: Rate;
  remediation_success_rate: Rate; first_action_acceptable: Rate; unsafe_action_rate: Rate; false_remediation_rate: Rate;
  outcome_match_rate: Rate; rollback_success_rate: Rate; human_intervention_rate: Rate;
  time_to_detect_s: Dur; detection_latency_s?: Dur; recovery_time_s: Dur; mttr_s: Dur; actions_per_incident: Dur;
  evidence_grounding: Dur; calibration_brier: number | null; by_fault_class: Record<string, { runs: number; passed: number }>;
};

export type Run = {
  id: string; mode: string; status: string; started_at: string; finished_at: string | null;
  metadata: Record<string, unknown>; summary: Summary | null;
};

export const RATE_ROWS: { key: keyof Summary; label: string; better: "higher" | "lower" }[] = [
  { key: "pass_rate", label: "Scenario pass rate", better: "higher" },
  { key: "detection_rate", label: "Detection rate", better: "higher" },
  { key: "root_cause_accuracy", label: "Root cause, top-1", better: "higher" },
  { key: "root_cause_top3", label: "Root cause, top-3", better: "higher" },
  { key: "remediation_success_rate", label: "Remediation success", better: "higher" },
  { key: "first_action_acceptable", label: "First action acceptable", better: "higher" },
  { key: "outcome_match_rate", label: "Outcome matches expectation", better: "higher" },
  { key: "rollback_success_rate", label: "Rollback success", better: "higher" },
  { key: "unsafe_action_rate", label: "Unsafe actions", better: "lower" },
  { key: "false_remediation_rate", label: "False remediations", better: "lower" },
  { key: "human_intervention_rate", label: "Needed a human", better: "lower" },
];

export function rateRows(s: Summary): RateRow[] {
  return RATE_ROWS.map(({ key, label, better }) => {
    const r = s[key] as Rate | undefined;
    return { label, better, value: r?.value ?? null, lo: r?.ci95?.[0] ?? 0, hi: r?.ci95?.[1] ?? 1, k: r?.k ?? 0, n: r?.n ?? 0 };
  });
}

export function fmtDur(d: Dur | undefined): string {
  if (!d || !d.n || d.mean === null) return "n/a";
  return `${d.median}s median, ${d.mean}s mean (n=${d.n}${d.ci95 ? `, 95% CI ${d.ci95[0]}–${d.ci95[1]}s` : ""})`;
}
