"use client";

import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Button, Chip, Empty, ErrorNote, Loading, Mono, Panel } from "@/components/ui";
import { api, fetcher } from "@/lib/api";
import { clock } from "@/lib/format";

type Entry = { id: number; ts: string; actor: string; action: string; resource: string; outcome: string;
  details: Record<string, unknown>; source: string; hash: string; prev_hash: string };
type Verify = { valid: boolean; checked: number; broken_at?: number; reason?: string; head?: string };

const PAGE = 100;

export default function AuditPage() {
  const [page, setPage] = useState(0);
  const [source, setSource] = useState("");
  const key = `/api/v1/audit?limit=${PAGE}&offset=${page * PAGE}${source ? `&source=${source}` : ""}`;
  const { data, error } = useSWR<{ items: Entry[] }>(key, fetcher);
  const [verify, setVerify] = useState<Verify | null>(null);
  const [vErr, setVErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function check() {
    setBusy(true);
    setVErr(null);
    try {
      setVerify(await api<Verify>("/api/v1/audit/verify"));
    } catch (e) {
      setVErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-xl font-semibold">Audit trail</h1>
          <p className="mt-1 max-w-3xl text-ink2">
            Append-only and hash-chained: each entry commits to the previous one, and the database rejects updates and deletes.
            Controller execution events are synced in from the control plane.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <select value={source} onChange={(e) => { setSource(e.target.value); setPage(0); }} aria-label="Source"
            className="rounded border border-line bg-panel px-2 py-1.5 text-sm">
            <option value="">All sources</option><option value="aegis">AegisOps API and engine</option>
            <option value="controller">Control plane (execution)</option>
          </select>
          <Button onClick={check} busy={busy}><ShieldCheck size={14} aria-hidden /> Verify chain</Button>
        </div>
      </div>
      {vErr ? <ErrorNote error={vErr} /> : null}
      {verify && (
        <p role="status" className={`rounded border px-3 py-2 text-sm ${verify.valid ? "border-good/40 bg-good/10" : "border-critical/50 bg-critical/10"}`}>
          {verify.valid ? `Chain intact: ${verify.checked} entries verified (head ${verify.head?.slice(0, 12)}).`
            : `Chain broken at entry ${verify.broken_at}: ${verify.reason}. Entries after it cannot be trusted.`}
        </p>
      )}
      {error ? <ErrorNote error={error} /> : !data ? <Loading label="Loading audit log" /> : (
        <Panel dense>
          {data.items.length === 0 ? <Empty>No entries.</Empty> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead><tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2 font-normal">#</th><th className="px-2 font-normal">Time</th>
                  <th className="px-2 font-normal">Actor</th><th className="px-2 font-normal">Action</th>
                  <th className="px-2 font-normal">Resource</th><th className="px-2 font-normal">Outcome</th>
                  <th className="px-4 font-normal">Hash</th></tr></thead>
                <tbody>
                  {data.items.map((e) => (
                    <tr key={e.id} className="border-b border-line/60 align-top">
                      <td className="tabular px-4 py-1.5 text-muted">{e.id}</td>
                      <td className="tabular px-2 text-ink2">{new Date(e.ts).toLocaleDateString()} {clock(e.ts)}</td>
                      <td className="max-w-[14rem] truncate px-2" title={e.actor}>{e.actor}</td>
                      <td className="px-2"><Mono>{e.action}</Mono></td>
                      <td className="max-w-[18rem] px-2">
                        <details>
                          <summary className="cursor-pointer truncate">{e.resource}</summary>
                          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11.5px] text-ink2">{JSON.stringify(e.details, null, 2)}</pre>
                        </details>
                      </td>
                      <td className="px-2"><Chip tone={/denied|rejected|failed/.test(e.outcome) ? "critical" : "neutral"} icon={false}>{e.outcome}</Chip></td>
                      <td className="px-4"><Mono className="text-muted" >{e.hash.slice(0, 12)}</Mono></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex justify-end gap-3 border-t border-line px-4 py-2 text-sm text-muted">
            <button disabled={page === 0} onClick={() => setPage(page - 1)} className="hover:text-ink disabled:opacity-40">Newer</button>
            <button disabled={data.items.length < PAGE} onClick={() => setPage(page + 1)} className="hover:text-ink disabled:opacity-40">Older</button>
          </div>
        </Panel>
      )}
    </div>
  );
}
