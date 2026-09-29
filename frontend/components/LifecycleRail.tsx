"use client";

import clsx from "clsx";
import { Undo2 } from "lucide-react";
import { clock } from "@/lib/format";
import type { IncidentDetail, TimelineEvent } from "@/lib/types";

type Stage = { key: string; label: string; at?: string; note?: string; state: "done" | "current" | "pending" | "skipped" | "failed" };

const ORDER = ["DETECTED", "INVESTIGATING", "DIAGNOSED", "AWAITING_APPROVAL", "EXECUTING", "VERIFYING"];

function firstTs(events: TimelineEvent[], pred: (e: TimelineEvent) => boolean) {
  return events.find(pred)?.ts;
}
function lastTs(events: TimelineEvent[], pred: (e: TimelineEvent) => boolean) {
  return [...events].reverse().find(pred)?.ts;
}
const statusTo = (to: string) => (e: TimelineEvent) => e.type === "status_changed" && e.data?.to === to;

/** The closed loop of one incident, reconstructed from recorded events (never inferred). */
export function LifecycleRail({ incident, events }: { incident: IncidentDetail; events: TimelineEvent[] }) {
  const status = incident.status;
  const terminal = status === "RESOLVED" || status === "ESCALATED";
  const currentIdx = ORDER.indexOf(status === "ROLLED_BACK" ? "VERIFYING" : status);
  const sim = incident.simulations.at(-1);
  const approval = incident.approvals.at(-1);
  const rollbacks = events.filter((e) => e.type === "rollback_started").length;

  const stageState = (idx: number, reached: boolean): Stage["state"] => {
    if (terminal) return reached ? "done" : "skipped";
    if (idx < currentIdx) return reached ? "done" : "skipped";
    if (idx === currentIdx) return "current";
    return "pending";
  };

  const stages: Stage[] = [
    { key: "detect", label: "Detect", at: firstTs(events, (e) => e.type === "incident_created"), state: "done" },
    { key: "investigate", label: "Investigate", at: firstTs(events, statusTo("INVESTIGATING")),
      note: incident.evidence_count ? `${incident.evidence_count} evidence` : undefined,
      state: stageState(1, !!firstTs(events, statusTo("INVESTIGATING"))) },
    { key: "diagnose", label: "Diagnose", at: lastTs(events, statusTo("DIAGNOSED")),
      note: incident.diagnosis?.selected ? `${Math.round((incident.diagnosis.confidence ?? 0) * 100)}% confident` : undefined,
      state: stageState(2, !!firstTs(events, statusTo("DIAGNOSED"))) },
    { key: "simulate", label: "Simulate", at: sim?.created_at, note: sim ? (sim.verdict ?? sim.phase) : "not needed",
      state: sim ? (terminal || currentIdx > 2 ? "done" : "current") : terminal || currentIdx > 2 ? "skipped" : "pending" },
    { key: "authorize", label: "Authorize",
      at: approval?.decided_at ?? firstTs(events, (e) => e.type === "policy_decision"),
      note: approval ? (approval.decided_by ? `${approval.status} by ${approval.decided_by}` : approval.status)
        : firstTs(events, (e) => e.type === "policy_decision") ? "policy" : undefined,
      state: status === "AWAITING_APPROVAL" ? "current"
        : firstTs(events, (e) => e.type === "policy_decision") ? "done" : terminal ? "skipped" : "pending" },
    { key: "execute", label: "Execute", at: firstTs(events, statusTo("EXECUTING")),
      state: stageState(4, !!firstTs(events, statusTo("EXECUTING"))) },
    { key: "verify", label: "Verify", at: lastTs(events, (e) => e.type === "verification_completed"),
      note: incident.verifications.at(-1)?.outcome?.toLowerCase(),
      state: stageState(5, !!firstTs(events, statusTo("VERIFYING"))) },
    { key: "outcome", label: status === "ESCALATED" ? "Escalated" : "Resolved",
      at: incident.resolved_at ?? lastTs(events, (e) => e.type === "incident_escalated"),
      note: incident.outcome?.replace(/_/g, " "),
      state: status === "RESOLVED" ? "done" : status === "ESCALATED" ? "failed" : "pending" },
  ];

  return (
    <div className="overflow-x-auto scrollbar-thin" aria-label="Incident lifecycle">
      <ol className="flex min-w-[760px] items-start">
        {stages.map((s, i) => (
          <li key={s.key} className="relative flex-1">
            {i > 0 && (
              <span aria-hidden className={clsx("absolute left-0 right-1/2 top-[9px] h-0.5 -translate-x-0",
                s.state === "done" || s.state === "current" || s.state === "failed" ? "bg-accent/70" : "bg-line")} />
            )}
            {i < stages.length - 1 && (
              <span aria-hidden className={clsx("absolute left-1/2 right-0 top-[9px] h-0.5",
                stages[i + 1].state === "done" || stages[i + 1].state === "current" || stages[i + 1].state === "failed"
                  ? "bg-accent/70" : "bg-line")} />
            )}
            <div className="relative flex flex-col items-center px-1 text-center">
              <span className={clsx("z-10 h-5 w-5 rounded-full border-2",
                s.state === "done" && "border-accent bg-accent",
                s.state === "current" && "rail-live border-accent bg-bg",
                s.state === "pending" && "border-line bg-bg",
                s.state === "skipped" && "border-dashed border-line bg-bg",
                s.state === "failed" && "border-critical bg-critical")} />
              <span className={clsx("mt-1.5 font-display text-sm font-semibold",
                s.state === "pending" || s.state === "skipped" ? "text-muted" : "text-ink")}>{s.label}</span>
              <span className="tabular text-xs text-ink2">{s.at ? clock(s.at) : s.state === "current" ? "in progress" : "–"}</span>
              {s.note && <span className="max-w-[9rem] truncate text-xs text-muted" title={s.note}>{s.note}</span>}
            </div>
          </li>
        ))}
      </ol>
      {(rollbacks > 0 || incident.iteration > 1) && (
        <p className="mt-3 flex items-center gap-2 text-xs text-ink2">
          <Undo2 size={13} className="text-serious" aria-hidden />
          {incident.iteration > 1 ? `Round ${incident.iteration} of the remediation loop. ` : ""}
          {rollbacks > 0 ? `${rollbacks} intervention${rollbacks > 1 ? "s were" : " was"} rolled back after failed verification.` : ""}
        </p>
      )}
    </div>
  );
}
