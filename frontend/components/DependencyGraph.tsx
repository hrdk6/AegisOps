"use client";

import { useMemo, useState } from "react";
import type { GraphEdge, GraphNode } from "@/lib/types";

const STATUS_STROKE: Record<string, string> = {
  healthy: "rgb(var(--good))", degraded: "rgb(var(--warning))", down: "rgb(var(--critical))", unknown: "rgb(var(--muted))",
};
const STATUS_GLYPH: Record<string, string> = { healthy: "✓", degraded: "!", down: "×", unknown: "?" };

type Props = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  root?: string | null;
  highlight?: string[];
  onSelect?: (name: string) => void;
  height?: number;
};

/** Layered layout: columns are dependency depth from the entry points (real edges only). */
function layout(nodes: GraphNode[], edges: GraphEdge[]) {
  const names = nodes.map((n) => n.name);
  const incoming = new Map<string, string[]>(names.map((n) => [n, []]));
  edges.forEach((e) => incoming.get(e.to)?.push(e.from));
  const level = new Map<string, number>();
  const visit = (n: string, stack: Set<string>): number => {
    if (level.has(n)) return level.get(n)!;
    if (stack.has(n)) return 0;
    stack.add(n);
    const parents = incoming.get(n) ?? [];
    const l = parents.length ? Math.max(...parents.map((p) => visit(p, stack) + 1)) : 0;
    stack.delete(n);
    level.set(n, l);
    return l;
  };
  names.forEach((n) => visit(n, new Set()));
  const columns = new Map<number, string[]>();
  names.forEach((n) => columns.set(level.get(n)!, [...(columns.get(level.get(n)!) ?? []), n]));
  columns.forEach((list) => list.sort());
  return { level, columns, depth: Math.max(0, ...level.values()) };
}

export function DependencyGraph({ nodes, edges, root, highlight = [], onSelect, height = 300 }: Props) {
  const [hover, setHover] = useState<string | null>(null);
  const { level, columns, depth } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const W = 920;
  const H = height;
  const nodeW = 128;
  const nodeH = 34;
  const pos = new Map<string, { x: number; y: number }>();
  columns.forEach((list, col) => {
    const x = 16 + (col * (W - nodeW - 32)) / Math.max(1, depth);
    list.forEach((n, i) => pos.set(n, { x, y: ((i + 1) * H) / (list.length + 1) - nodeH / 2 }));
  });
  const related = hover ? new Set(edges.filter((e) => e.from === hover || e.to === hover).flatMap((e) => [e.from, e.to])) : null;
  const statusOf = new Map(nodes.map((n) => [n.name, n.status ?? "unknown"]));

  return (
    <figure>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img"
        aria-label={`Service dependency graph with ${nodes.length} services and ${edges.length} dependencies`}>
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="rgb(var(--muted))" />
          </marker>
          <marker id="arrow-hot" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="rgb(var(--serious))" />
          </marker>
        </defs>
        {edges.map((e) => {
          const a = pos.get(e.from);
          const b = pos.get(e.to);
          if (!a || !b) return null;
          const x1 = a.x + nodeW;
          const y1 = a.y + nodeH / 2;
          const x2 = b.x;
          const y2 = b.y + nodeH / 2;
          const back = (level.get(e.to) ?? 0) <= (level.get(e.from) ?? 0);
          const mid = (x1 + x2) / 2;
          const d = back
            ? `M${a.x + nodeW / 2},${a.y + nodeH} C${a.x + nodeW / 2},${a.y + nodeH + 40} ${b.x + nodeW / 2},${b.y + nodeH + 40} ${b.x + nodeW / 2},${b.y + nodeH}`
            : `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
          const dim = related && !(related.has(e.from) && related.has(e.to));
          const unhealthy = statusOf.get(e.to) === "down" || statusOf.get(e.to) === "degraded";
          const observed = e.observedMetrics || e.observedTraces;
          const stroke = unhealthy ? "rgb(var(--serious))" : "rgb(var(--muted))";
          return (
            <g key={`${e.from}-${e.to}`} opacity={dim ? 0.15 : 1} style={{ transition: "opacity 200ms ease" }}>
              <path d={d} fill="none" markerEnd={unhealthy ? "url(#arrow-hot)" : "url(#arrow)"} stroke={stroke}
                strokeOpacity={0.55} strokeWidth={unhealthy ? 1.6 : 1.2}
                strokeDasharray={e.declared && !observed ? "5 4" : !e.declared ? "2 3" : undefined}>
                <title>{`${e.from} → ${e.to} (${e.status})`}</title>
              </path>
              {/* Moving dashes on edges seen in telemetry: traffic, flowing in the direction of the call. */}
              {observed && !back && (
                <path d={d} fill="none" stroke={unhealthy ? "rgb(var(--serious))" : "rgb(var(--accent))"} strokeWidth={2}
                  strokeLinecap="round" className="edge-flow" opacity={0.9} aria-hidden />
              )}
            </g>
          );
        })}
        {nodes.map((n) => {
          const p = pos.get(n.name)!;
          const status = n.status ?? "unknown";
          const isRoot = root === n.name;
          const hl = highlight.includes(n.name);
          const dim = related && !related.has(n.name);
          return (
            <g key={n.name} transform={`translate(${p.x},${p.y})`} opacity={dim ? 0.35 : 1}
              onMouseEnter={() => setHover(n.name)} onMouseLeave={() => setHover(null)}
              onClick={() => onSelect?.(n.name)} style={{ cursor: onSelect ? "pointer" : "default", transition: "opacity 200ms ease" }}
              tabIndex={onSelect ? 0 : -1} onKeyDown={(ev) => ev.key === "Enter" && onSelect?.(n.name)}
              role={onSelect ? "button" : undefined} aria-label={`${n.name}: ${status}${isRoot ? ", probable root cause" : ""}`}>
              {isRoot && <rect x={-6} y={-6} width={nodeW + 12} height={nodeH + 12} rx={13} fill="rgb(var(--accent) / 0.06)"
                stroke="rgb(var(--accent))" strokeWidth={1.8} strokeDasharray="5 4" />}
              <rect width={nodeW} height={nodeH} rx={9} fill={hl ? "rgb(var(--raised))" : "rgb(var(--panel))"}
                stroke={STATUS_STROKE[status] ?? STATUS_STROKE.unknown} strokeWidth={status === "healthy" ? 1 : 1.8}
                strokeOpacity={status === "healthy" ? 0.55 : 1}
                style={status !== "healthy" && status !== "unknown"
                  ? { filter: `drop-shadow(0 0 6px ${STATUS_STROKE[status].replace(")", " / 0.55)")})` } : undefined} />
              <circle cx={16} cy={nodeH / 2} r={7} fill={STATUS_STROKE[status] ?? STATUS_STROKE.unknown} />
              <text x={16} y={nodeH / 2 + 3.5} textAnchor="middle" fontSize="10" fontWeight="800" fill="rgb(var(--bg))">
                {STATUS_GLYPH[status] ?? "?"}
              </text>
              <text x={30} y={nodeH / 2 + 4} fontSize="12.5" fontWeight={status === "healthy" ? 500 : 600} fill="rgb(var(--ink))"
                fontFamily="var(--font-archivo)">
                {n.name.length > 16 ? `${n.name.slice(0, 15)}…` : n.name}
              </text>
              <title>{`${n.name}: ${status}${n.replicas !== undefined ? ` (${n.ready}/${n.replicas} ready)` : ""}`}</title>
            </g>
          );
        })}
      </svg>
      <figcaption className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span><span aria-hidden className="mr-1 inline-block h-0.5 w-4 bg-accent align-middle" />Moving: traffic seen in telemetry</span>
        <span>Solid: declared and observed in telemetry</span>
        <span>Dashed: declared, not yet observed</span>
        <span>Dotted: observed, not declared</span>
        {root && <span>Dashed ring: probable root cause</span>}
      </figcaption>
    </figure>
  );
}
