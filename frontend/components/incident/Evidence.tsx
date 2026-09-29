"use client";

import clsx from "clsx";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";
import { Sparkline } from "@/components/charts";
import { Empty, Mono, Tabs } from "@/components/ui";
import { ago } from "@/lib/format";
import type { Evidence } from "@/lib/types";

const KIND_LABEL: Record<string, string> = {
  metric: "Metrics", log: "Logs", trace: "Traces", change: "Changes", k8s_state: "Kubernetes", k8s_event: "Events",
  topology: "Topology", runbook: "Runbooks", history: "History", canary: "Canary",
};

export function EvidenceItem({ ev, highlight }: { ev: Evidence; highlight?: boolean }) {
  const [open, setOpen] = useState(false);
  const series = ev.data?.series as [number, number][] | undefined;
  const threshold = (ev.data?.threshold as number | undefined) ?? null;
  return (
    <li id={ev.id} className={clsx("px-4 py-3", highlight && "bg-accent/5 ring-1 ring-inset ring-accent/40")}>
      <div className="flex items-start gap-3">
        <button onClick={() => setOpen(!open)} className="mt-0.5 text-muted hover:text-ink" aria-expanded={open}
          aria-label={open ? "Hide provenance" : "Show provenance"}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2">
            <span className="font-medium text-ink">{ev.title}</span>
          </div>
          <p className="mt-0.5 text-sm text-ink2">{ev.summary}</p>
          <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted">
            <Mono>{ev.id}</Mono>
            <span>{KIND_LABEL[ev.kind] ?? ev.kind}{ev.service ? `, ${ev.service}` : ""}</span>
            <span>from {ev.source.system}</span>
            <span>observed {ago(ev.observed_at)}</span>
            <span>relevance {(ev.score * 100).toFixed(0)}</span>
          </div>
          {open && (
            <div className="mt-2 space-y-1 rounded border border-line bg-bg p-2 text-xs">
              {ev.source.query && <div className="break-all"><span className="text-muted">Query </span><Mono>{ev.source.query}</Mono></div>}
              {ev.source.url && <div className="break-all"><span className="text-muted">Link </span><Mono>{ev.source.url}</Mono></div>}
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap font-mono text-[11.5px] text-ink2">
                {JSON.stringify(Object.fromEntries(Object.entries(ev.data).filter(([k]) => k !== "series")), null, 2)}
              </pre>
            </div>
          )}
        </div>
        {series && series.length > 1 && <Sparkline points={series} threshold={threshold} />}
      </div>
    </li>
  );
}

export function EvidenceExplorer({ evidence, highlighted }: { evidence: Evidence[]; highlighted: Set<string> }) {
  const kinds = useMemo(() => {
    const counts = new Map<string, number>();
    evidence.forEach((e) => counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [evidence]);
  const [tab, setTab] = useState<string>("cited");
  const items = tab === "cited" ? evidence.filter((e) => highlighted.has(e.id))
    : tab === "all" ? evidence : evidence.filter((e) => e.kind === tab);
  return (
    <div>
      <Tabs value={tab} onChange={setTab}
        tabs={[{ id: "cited", label: "Cited by diagnosis", count: highlighted.size }, { id: "all", label: "All", count: evidence.length },
          ...kinds.map(([k, n]) => ({ id: k, label: KIND_LABEL[k] ?? k, count: n }))]} />
      {items.length === 0 ? <Empty>No evidence in this category.</Empty> : (
        <ul className="max-h-[560px] divide-y divide-line overflow-y-auto scrollbar-thin">
          {items.map((e) => <EvidenceItem key={e.id} ev={e} highlight={highlighted.has(e.id) && tab !== "cited"} />)}
        </ul>
      )}
    </div>
  );
}
