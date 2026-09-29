"use client";

import clsx from "clsx";
import { useState } from "react";
import { Empty } from "@/components/ui";
import { clock } from "@/lib/format";
import type { TimelineEvent } from "@/lib/types";

// Event types grouped by what they mean for an operator reading the record.
const TONE: Record<string, string> = {
  incident_created: "bg-serious", anomaly_detected: "bg-serious", symptom_added: "bg-serious",
  root_cause_identified: "bg-accent", remediation_proposed: "bg-accent", simulation_completed: "bg-accent",
  approval_requested: "bg-serious", approval_decided: "bg-accent", policy_decision: "bg-muted",
  action_started: "bg-accent", action_completed: "bg-good", verification_completed: "bg-accent",
  action_denied: "bg-critical", action_failed: "bg-critical", rollback_started: "bg-critical", rollback_completed: "bg-serious",
  incident_escalated: "bg-critical", incident_resolved: "bg-good", relapse_detected: "bg-critical",
  rollback_failed: "bg-critical", action_rejected: "bg-critical", recurrence_watch_passed: "bg-good",
  investigation_extended: "bg-muted", workflow_resumed: "bg-muted",
};
const NOISY = new Set(["anomaly_detected", "evidence_collected", "action_updated", "status_changed"]);

export function Timeline({ events }: { events: TimelineEvent[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? events : events.filter((e, i) => !NOISY.has(e.type) || i === 0
    || (e.type === "status_changed" && ["RESOLVED", "ESCALATED", "AWAITING_APPROVAL"].includes(String(e.data?.to))));
  if (!events.length) return <Empty>No events recorded yet.</Empty>;
  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-xs text-muted">
        <span>{shown.length} of {events.length} events</span>
        <button onClick={() => setAll(!all)} className="text-accent hover:underline">{all ? "Key events only" : "Show every event"}</button>
      </div>
      <ol className="relative space-y-0 border-l border-line pl-4">
        {shown.map((e) => (
          <li key={e.id} className="relative pb-3">
            <span aria-hidden className={clsx("absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-panel",
              TONE[e.type] ?? "bg-line")} />
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="tabular text-xs text-muted">{clock(e.ts)}</span>
              <span className="text-xs text-ink2">{e.type.replace(/_/g, " ")}</span>
              <span className="text-xs text-muted">by {e.actor}</span>
            </div>
            <p className="text-sm text-ink">{e.message}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}
