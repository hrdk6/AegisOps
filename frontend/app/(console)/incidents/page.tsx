"use client";

import clsx from "clsx";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { Empty, ErrorNote, IncidentStatus, Loading, Mono, Panel, Severity } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, duration, humanize } from "@/lib/format";
import type { IncidentSummary } from "@/lib/types";

const FILTERS = [
  { id: "", label: "All" }, { id: "active", label: "Active" }, { id: "RESOLVED", label: "Resolved" },
  { id: "ESCALATED", label: "Escalated" },
] as const;
const PAGE = 25;

export default function IncidentsPage() {
  const [filter, setFilter] = useState<string>("");
  const [page, setPage] = useState(0);
  const key = `/api/v1/incidents?limit=${PAGE}&offset=${page * PAGE}${filter ? `&status=${filter}` : ""}`;
  const { data, error } = useSWR<{ items: IncidentSummary[]; total: number }>(key, fetcher);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-xl font-semibold">Incidents</h1>
          <p className="mt-1 text-ink2">Every incident AegisOps opened, with its diagnosis and how it ended.</p>
        </div>
        <div role="radiogroup" aria-label="Filter incidents" className="flex rounded border border-line bg-panel p-0.5">
          {FILTERS.map((f) => (
            <button key={f.id} role="radio" aria-checked={filter === f.id} onClick={() => { setFilter(f.id); setPage(0); }}
              className={clsx("rounded-sm px-3 py-1 text-sm", filter === f.id ? "bg-raised text-ink" : "text-muted hover:text-ink2")}>
              {f.label}
            </button>
          ))}
        </div>
      </div>
      {error ? <ErrorNote error={error} /> : !data ? <Loading label="Loading incidents" /> : (
        <Panel dense>
          {data.items.length === 0 ? <Empty>No incidents match this filter.</Empty> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="px-4 py-2 font-normal">Severity</th><th className="px-2 font-normal">Incident</th>
                    <th className="px-2 font-normal">Status</th><th className="px-2 font-normal">Probable cause</th>
                    <th className="px-2 font-normal">Outcome</th><th className="px-2 text-right font-normal">Detected</th>
                    <th className="px-4 text-right font-normal">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((i) => (
                    <tr key={i.id} className="border-b border-line/60 hover:bg-raised/50">
                      <td className="px-4 py-2.5"><Severity severity={i.severity} /></td>
                      <td className="max-w-[28rem] px-2">
                        <Link href={`/incidents/${i.id}`} className="block truncate font-medium hover:text-accent">{i.title}</Link>
                        <Mono className="text-muted">{i.id}</Mono>
                      </td>
                      <td className="px-2"><IncidentStatus status={i.status} /></td>
                      <td className="px-2 text-ink2">
                        {i.root_service ? <>{humanize(i.category)} on {i.root_service}
                          {i.confidence ? <span className="text-muted"> ({Math.round(i.confidence * 100)}%)</span> : null}</> : "–"}
                      </td>
                      <td className="px-2 text-ink2">{humanize(i.outcome)}</td>
                      <td className="tabular px-2 text-right text-ink2">{ago(i.detected_at)}</td>
                      <td className="tabular px-4 text-right">{duration(i.duration_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex items-center justify-between border-t border-line px-4 py-2 text-sm text-muted">
            <span>{data.total} incident{data.total === 1 ? "" : "s"}</span>
            <div className="flex gap-2">
              <button disabled={page === 0} onClick={() => setPage(page - 1)} className="hover:text-ink disabled:opacity-40">Newer</button>
              <button disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}
                className="hover:text-ink disabled:opacity-40">Older</button>
            </div>
          </div>
        </Panel>
      )}
    </div>
  );
}
