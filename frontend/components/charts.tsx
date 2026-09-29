"use client";

import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipProps,
} from "recharts";

type Point = [number, number];

function timeTick(t: number) {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

function ChartTooltip({ active, payload, label, format }: TooltipProps<number, string> & { format: (v: number) => string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-line bg-raised px-2.5 py-1.5 text-xs shadow-lg">
      <div className="text-muted">{new Date(label as number).toLocaleTimeString([], { hour12: false })}</div>
      <div className="tabular font-semibold text-ink">{format(payload[0].value as number)}</div>
    </div>
  );
}

/** One measure per chart (no dual axes); an optional SLO threshold as a reference line. */
export function TimeSeries({ title, points, format, threshold, thresholdLabel = "SLO", height = 150, color = "var(--series-1)" }: {
  title: string; points: Point[]; format: (v: number) => string; threshold?: number | null; thresholdLabel?: string;
  height?: number; color?: string;
}) {
  const data = points.map(([t, v]) => ({ t: t * 1000, v }));
  const last = data.at(-1)?.v;
  return (
    <figure className="min-w-0">
      <figcaption className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-sm text-ink2">{title}</span>
        <span className="tabular font-display text-md font-semibold text-ink">{last !== undefined ? format(last) : "–"}</span>
      </figcaption>
      {data.length < 2 ? (
        <div className="flex items-center justify-center rounded border border-dashed border-line text-xs text-muted" style={{ height }}>
          No samples in this window
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={timeTick} stroke="var(--axis)"
              tick={{ fill: "rgb(var(--muted))", fontSize: 11 }} tickLine={false} minTickGap={40} />
            <YAxis width={52} tickFormatter={(v) => format(v)} stroke="var(--axis)" tick={{ fill: "rgb(var(--muted))", fontSize: 11 }}
              tickLine={false} axisLine={false} domain={[0, "auto"]} />
            <Tooltip content={<ChartTooltip format={format} />} cursor={{ stroke: "rgb(var(--muted))", strokeDasharray: "3 3" }} />
            {threshold !== undefined && threshold !== null && (
              <ReferenceLine y={threshold} stroke="rgb(var(--serious))" strokeDasharray="5 4" strokeWidth={1.2}
                label={{ value: thresholdLabel, position: "insideTopRight", fill: "rgb(var(--ink2))", fontSize: 11 }} />
            )}
            <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} isAnimationActive={false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "rgb(var(--panel))" }} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </figure>
  );
}

/** Compact inline trend for evidence items; the value and threshold are stated in text beside it. */
export function Sparkline({ points, threshold, width = 160, height = 34 }: { points: Point[]; threshold?: number | null;
  width?: number; height?: number }) {
  if (points.length < 2) return null;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const maxY = Math.max(...ys, threshold ?? 0) || 1;
  const x = (t: number) => ((t - xs[0]) / Math.max(1, xs.at(-1)! - xs[0])) * (width - 4) + 2;
  const y = (v: number) => height - 3 - (v / maxY) * (height - 6);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  return (
    <svg width={width} height={height} aria-hidden className="shrink-0">
      {threshold ? <line x1={0} x2={width} y1={y(threshold)} y2={y(threshold)} stroke="rgb(var(--serious))"
        strokeDasharray="3 3" strokeWidth={1} /> : null}
      <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={1.6} strokeLinejoin="round" />
    </svg>
  );
}

export type RateRow = { label: string; value: number | null; lo: number; hi: number; k: number; n: number; better: "higher" | "lower" };

/** Forest plot: point estimate with its 95% confidence interval on a 0–100% scale. */
export function RateIntervals({ rows }: { rows: RateRow[] }) {
  const W = 260;
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">Benchmark rates with 95% Wilson confidence intervals</caption>
      <thead>
        <tr className="text-left text-xs text-muted">
          <th className="py-1 font-normal">Metric</th>
          <th className="py-1 font-normal">Estimate (95% CI)</th>
          <th className="py-1 font-normal" aria-hidden>
            <svg width={W} height={12}>
              {[0, 0.25, 0.5, 0.75, 1].map((t) => (
                <text key={t} x={4 + t * (W - 8)} y={10} fontSize="10" textAnchor="middle" fill="rgb(var(--muted))">{t * 100}%</text>
              ))}
            </svg>
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} className="border-t border-line/60">
            <td className="py-1.5 pr-3 text-ink2">{r.label}{r.better === "lower" ? " (lower is better)" : ""}</td>
            <td className="tabular py-1.5 pr-3">
              {r.value === null ? "n/a" : `${Math.round(r.value * 100)}%`}
              <span className="text-xs text-muted"> {r.k}/{r.n} · {Math.round(r.lo * 100)}–{Math.round(r.hi * 100)}%</span>
            </td>
            <td className="py-1.5">
              <svg width={W} height={16} role="img" aria-label={`${r.label}: ${r.value === null ? "no data" : Math.round(r.value * 100) + "%"}`}>
                <line x1={4} x2={W - 4} y1={8} y2={8} stroke="var(--grid)" strokeWidth={1} />
                {r.value !== null && (
                  <>
                    <line x1={4 + r.lo * (W - 8)} x2={4 + r.hi * (W - 8)} y1={8} y2={8} stroke="var(--series-1)" strokeWidth={3}
                      strokeLinecap="round" opacity={0.45} />
                    <circle cx={4 + r.value * (W - 8)} cy={8} r={4.5} fill="var(--series-1)" stroke="rgb(var(--panel))" strokeWidth={2} />
                  </>
                )}
              </svg>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
