"use client";

import { ArrowLeft, Download } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { Button, Chip, ErrorNote, Loading, Mono, Panel } from "@/components/ui";
import { api, fetcher } from "@/lib/api";
import { clock, duration, humanize, pct } from "@/lib/format";

type Claim = { type: "FACT" | "HYPOTHESIS" | "MODEL_INTERPRETATION"; text: string; evidence: string[] };
type Postmortem = {
  incident_id: string; generated_at: string; method: string; markdown: string;
  content: {
    title: string; severity: string; status: string; outcome: string | null; onset: string; detected_at: string;
    resolved_at: string | null; duration_seconds: number;
    impact: { failed_requests?: number; total_requests?: number; failure_ratio?: number; window_seconds?: number } | null;
    sections: { title: string; claims: Claim[] }[];
  };
};

const CLAIM = {
  FACT: { tone: "good" as const, label: "Fact", hint: "Directly observed and recorded; cites evidence." },
  HYPOTHESIS: { tone: "warning" as const, label: "Hypothesis", hint: "Inferred from evidence; may be wrong." },
  MODEL_INTERPRETATION: { tone: "accent" as const, label: "Model interpretation", hint: "Written by a language model; not verified." },
};

export default function PostmortemPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error } = useSWR<Postmortem>(`/api/v1/incidents/${id}/postmortem`, fetcher);

  async function download() {
    const md = await api<string>(`/api/v1/incidents/${id}/postmortem?format=markdown`);
    const url = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `${id}-postmortem.md` });
    a.click();
    URL.revokeObjectURL(url);
  }

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading label="Loading postmortem" />;
  const c = data.content;

  return (
    <article className="mx-auto max-w-4xl space-y-5">
      <div>
        <Link href={`/incidents/${id}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft size={14} aria-hidden /> {id}
        </Link>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="font-display text-xl font-semibold">Postmortem: {c.title}</h1>
            <p className="mt-1 text-sm text-ink2">
              {c.severity} · outcome {humanize(c.outcome)} · onset {clock(c.onset)} · lasted {duration(c.duration_seconds)} ·
              generated {clock(data.generated_at)} ({data.method === "template" ? "deterministic template" : data.method})
            </p>
          </div>
          <Button onClick={download}><Download size={14} aria-hidden /> Markdown</Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-4 rounded-lg border border-line bg-panel p-3 text-sm">
        {Object.entries(CLAIM).map(([k, v]) => (
          <span key={k} className="flex items-center gap-2"><Chip tone={v.tone}>{v.label}</Chip><span className="text-ink2">{v.hint}</span></span>
        ))}
      </div>

      {c.impact && (
        <Panel title="Customer impact">
          <dl className="grid gap-4 sm:grid-cols-3">
            <div><dt className="text-xs text-muted">Failed requests at the entry point</dt>
              <dd className="tabular font-display text-lg font-semibold">{c.impact.failed_requests ?? "–"}</dd></div>
            <div><dt className="text-xs text-muted">Of all requests</dt>
              <dd className="tabular font-display text-lg font-semibold">{c.impact.total_requests ?? "–"}</dd></div>
            <div><dt className="text-xs text-muted">Failure ratio over the incident</dt>
              <dd className="tabular font-display text-lg font-semibold">{pct(c.impact.failure_ratio)}</dd></div>
          </dl>
        </Panel>
      )}

      {c.sections.map((s) => (
        <Panel key={s.title} title={s.title}>
          <ul className="space-y-2.5">
            {s.claims.map((cl, i) => (
              <li key={i} className="flex gap-3">
                <Chip tone={CLAIM[cl.type]?.tone ?? "neutral"} className="mt-0.5 h-fit">{CLAIM[cl.type]?.label ?? cl.type}</Chip>
                <div className="min-w-0">
                  <p className="text-sm text-ink">{cl.text}</p>
                  {cl.evidence.length > 0 && (
                    <p className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-muted">
                      evidence: {cl.evidence.map((e) => <Mono key={e}>{e}</Mono>)}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      ))}
    </article>
  );
}
