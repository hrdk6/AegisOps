"use client";

import { ArrowLeft, FileText } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import useSWR from "swr";
import { DependencyGraph } from "@/components/DependencyGraph";
import { EvidenceExplorer } from "@/components/incident/Evidence";
import { ActionsPanel, ApprovalPanel, DiagnosisPanel, RemediationPanel, SimulationPanel } from "@/components/incident/Panels";
import { Timeline } from "@/components/incident/Timeline";
import { LifecycleRail } from "@/components/LifecycleRail";
import { Button, Chip, Empty, ErrorNote, IncidentStatus, Loading, Mono, Panel, Severity } from "@/components/ui";
import { api, can, fetcher } from "@/lib/api";
import { ago, clock, duration } from "@/lib/format";
import type { Evidence, IncidentDetail, TimelineEvent, Topology } from "@/lib/types";

export default function IncidentPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: inc, error, mutate } = useSWR<IncidentDetail>(`/api/v1/incidents/${id}`, fetcher);
  const { data: tl } = useSWR<{ items: TimelineEvent[] }>(`/api/v1/incidents/${id}/timeline`, fetcher);
  const { data: ev } = useSWR<{ items: Evidence[] }>(`/api/v1/incidents/${id}/evidence`, fetcher);
  const { data: topo } = useSWR<Topology>("/api/v1/topology", fetcher, { refreshInterval: 30000 });
  const [busy, setBusy] = useState<string | null>(null);
  const [opError, setOpError] = useState<unknown>(null);

  const cited = useMemo(() => new Set((inc?.diagnosis?.hypotheses ?? [])
    .flatMap((h) => [...h.supporting_evidence, ...h.contradicting_evidence])), [inc?.diagnosis]);

  if (error) return <ErrorNote error={error} />;
  if (!inc) return <Loading label={`Loading ${id}`} />;

  const terminal = inc.status === "RESOLVED" || inc.status === "ESCALATED";
  const pending = inc.approvals.find((a) => a.status === "pending");
  const events = tl?.items ?? [];

  async function command(kind: "investigate" | "resolve" | "escalate") {
    setBusy(kind);
    setOpError(null);
    try {
      await api(`/api/v1/incidents/${id}/${kind}`, { method: "POST", json: kind === "investigate" ? undefined : { note: "" } });
      await mutate();
    } catch (e) {
      setOpError(e);
    } finally {
      setBusy(null);
    }
  }

  function cite(evId: string) {
    document.getElementById(evId)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <div className="space-y-5">
      <div>
        <Link href="/incidents" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft size={14} aria-hidden /> Incidents
        </Link>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Severity severity={inc.severity} />
              <IncidentStatus status={inc.status} />
              <Mono className="text-muted">{inc.id}</Mono>
            </div>
            <h1 className="mt-1.5 font-display text-xl font-semibold">{inc.title}</h1>
            <p className="mt-1 text-sm text-ink2">
              Anomaly onset {clock(inc.onset_at)}, detected {ago(inc.detected_at)}
              {inc.resolved_at ? `, closed at ${clock(inc.resolved_at)}` : ""} · {terminal ? "lasted" : "running for"} {duration(inc.duration_seconds)}
              {inc.human_interventions ? ` · ${inc.human_interventions} human decision${inc.human_interventions > 1 ? "s" : ""}` : ""}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {inc.affected_services.map((s) => (
                <Link key={s} href={`/services/${s}`}>
                  <Chip tone={s === inc.root_service ? "critical" : "neutral"} icon={false}>{s}{s === inc.root_service ? " · probable root" : ""}</Chip>
                </Link>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {inc.has_postmortem && (
              <Button onClick={() => router.push(`/incidents/${id}/postmortem`)}><FileText size={14} aria-hidden /> Postmortem</Button>
            )}
            {terminal && can("investigate") && (
              <Button busy={busy === "investigate"} onClick={() => command("investigate")}>Re-investigate</Button>
            )}
            {!terminal && can("resolve") && (
              <Button busy={busy === "escalate"} onClick={() => command("escalate")}>Escalate to on-call</Button>
            )}
            {inc.status !== "RESOLVED" && can("resolve") && (
              <Button busy={busy === "resolve"} onClick={() => command("resolve")}>Mark resolved</Button>
            )}
          </div>
        </div>
        {opError ? <div className="mt-3"><ErrorNote error={opError} /></div> : null}
      </div>

      <Panel title="Remediation loop"><LifecycleRail incident={inc} events={events} /></Panel>

      {pending && (
        <Panel title="Approval required" className="border-serious/60">
          <ApprovalPanel approval={pending} canApprove={can("approve")} onDone={() => mutate()} />
        </Panel>
      )}

      <div className="grid gap-5 xl:grid-cols-3">
        <div className="min-w-0 space-y-5 xl:col-span-2">
          <Panel title="Diagnosis" action={inc.diagnosis ? <span className="text-xs text-muted">
            {inc.diagnosis.method === "rules" ? "deterministic rules" : "rules fused with model"} · round {inc.diagnosis.iteration}</span> : null}>
            {inc.diagnosis ? <DiagnosisPanel dx={inc.diagnosis} onCite={cite} /> : <Empty>Investigation in progress.</Empty>}
          </Panel>
          <Panel title="Remediation candidates">
            {inc.plan ? <RemediationPanel plan={inc.plan} /> : <Empty>No plan yet.</Empty>}
          </Panel>
          <Panel title="Sandbox simulation"><SimulationPanel sims={inc.simulations} /></Panel>
          <Panel title="Actions and verification"><ActionsPanel actions={inc.actions} verifications={inc.verifications} /></Panel>
          <Panel title="Evidence" dense>
            {ev ? <EvidenceExplorer evidence={ev.items} highlighted={cited} /> : <Loading label="Loading evidence" />}
          </Panel>
        </div>
        <div className="min-w-0 space-y-5">
          <Panel title="Blast radius">
            {topo ? (
              <DependencyGraph nodes={topo.nodes} edges={topo.edges} root={inc.root_service} highlight={inc.affected_services}
                onSelect={(n) => router.push(`/services/${n}`)} height={300} />
            ) : <Loading label="Loading graph" />}
            {inc.diagnosis?.blast_radius?.length ? (
              <p className="mt-2 text-xs text-ink2">Upstream of the probable root: {inc.diagnosis.blast_radius.join(", ")}.</p>
            ) : null}
          </Panel>
          <Panel title="Timeline"><Timeline events={events} /></Panel>
        </div>
      </div>
    </div>
  );
}
