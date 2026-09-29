"use client";

import Link from "next/link";
import { ago, duration } from "@/lib/format";
import type { IncidentSummary } from "@/lib/types";
import { IncidentStatus, Severity } from "./ui";

const STAGES = ["DETECTED", "INVESTIGATING", "DIAGNOSED", "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "RESOLVED"];

/** Flight-strip style row: severity, identity, where it is in the loop, and how long it has run. */
export function IncidentRow({ incident: i }: { incident: IncidentSummary }) {
  const idx = i.status === "ESCALATED" ? STAGES.length - 1 : Math.max(0, STAGES.indexOf(i.status === "ROLLED_BACK" ? "VERIFYING" : i.status));
  return (
    <li>
      <Link href={`/incidents/${i.id}`} className="block px-4 py-3.5 transition-colors hover:bg-raised/60">
        <div className="flex items-center gap-2">
          <Severity severity={i.severity} />
          <span className="truncate font-medium text-ink">{i.title}</span>
        </div>
        <div className="mt-1.5 flex items-center gap-3 text-xs text-ink2">
          <IncidentStatus status={i.status} />
          <span className="whitespace-nowrap font-mono">{i.id}</span>
          <span className="ml-auto whitespace-nowrap">{ago(i.detected_at)} · running {duration(i.duration_seconds)}</span>
        </div>
        <div className="mt-2 flex gap-0.5" aria-hidden>
          {STAGES.map((s, k) => (
            <span key={s} className={`h-1.5 flex-1 rounded-full ${k <= idx ? (i.status === "ESCALATED" ? "bg-critical" : "bg-gradient-to-r from-accent2 to-accent") : "bg-line"} ${k === idx && i.status !== "RESOLVED" && i.status !== "ESCALATED" ? "animate-pulse" : ""}`} />
          ))}
        </div>
        {i.root_service && (
          <p className="mt-1.5 truncate text-xs text-muted">
            Probable cause: {i.category?.replace(/_/g, " ").toLowerCase()} on {i.root_service}
            {i.confidence ? ` (${Math.round(i.confidence * 100)}%)` : ""}
          </p>
        )}
      </Link>
    </li>
  );
}
