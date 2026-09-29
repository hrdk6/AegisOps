"use client";

import clsx from "clsx";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import type { IncidentSummary } from "@/lib/types";

/** The remediation loop, drawn as the closed loop it is. Stations run clockwise from the top. */
export const LOOP_STATIONS = ["Detect", "Investigate", "Diagnose", "Simulate", "Authorize", "Execute", "Verify"] as const;

// Where an incident status sits on the loop. Simulation has no status of its own; it happens while DIAGNOSED.
const STATION_OF: Record<string, number> = {
  DETECTED: 0, INVESTIGATING: 1, DIAGNOSED: 2, AWAITING_APPROVAL: 4, EXECUTING: 5, VERIFYING: 6, ROLLED_BACK: 6,
};
const SEV_COLOR: Record<string, string> = { SEV1: "var(--critical)", SEV2: "var(--serious)", SEV3: "var(--warning)" };

const C = 160;
const R = 116;
const LABEL_R = 142;
const TOTAL = LOOP_STATIONS.length;
// Rounded so server- and client-rendered coordinates match exactly.
const round = (n: number) => Math.round(n * 100) / 100;
const angle = (i: number) => ((-90 + (i * 360) / TOTAL) * Math.PI) / 180;
const at = (i: number, r: number) => ({ x: round(C + r * Math.cos(angle(i))), y: round(C + r * Math.sin(angle(i))) });

type Props = {
  incidents?: IncidentSummary[];
  /** Cycle a sample incident around the loop (sign-in screen). */
  demo?: boolean;
  children?: ReactNode;
  className?: string;
};

export function LoopDial({ incidents = [], demo, children, className }: Props) {
  const [demoStation, setDemoStation] = useState(0);
  useEffect(() => {
    if (!demo) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const t = window.setInterval(() => setDemoStation((s) => (s + 1) % TOTAL), 1500);
    return () => window.clearInterval(t);
  }, [demo]);

  const placed = incidents
    .map((i) => ({ i, station: STATION_OF[i.status] }))
    .filter((p): p is { i: IncidentSummary; station: number } => p.station !== undefined);
  const busy = new Set(demo ? [demoStation] : placed.map((p) => p.station));
  const circumference = 2 * Math.PI * R;

  return (
    <div className={clsx("relative", className)}>
      <svg viewBox="-70 -6 460 332" className="h-auto w-full overflow-visible" role="img"
        aria-label={demo ? "The AegisOps remediation loop"
          : `Remediation loop: ${placed.length ? placed.map((p) => `${p.i.id} at ${LOOP_STATIONS[p.station]}`).join(", ") : "no incident in progress"}`}>
        <defs>
          <linearGradient id="loop-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="rgb(var(--accent2))" />
            <stop offset="1" stopColor="rgb(var(--accent))" />
          </linearGradient>
          <radialGradient id="loop-core" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stopColor="rgb(var(--glow) / 0.16)" />
            <stop offset="1" stopColor="rgb(var(--glow) / 0)" />
          </radialGradient>
        </defs>

        <circle cx={C} cy={C} r={R - 14} fill="url(#loop-core)" />
        {/* Tick marks: a fine dial between stations. */}
        {Array.from({ length: TOTAL * 6 }, (_, k) => {
          const a = ((-90 + (k * 360) / (TOTAL * 6)) * Math.PI) / 180;
          const r1 = R - 9;
          const r2 = k % 6 === 0 ? R - 16 : R - 12;
          return <line key={k} x1={round(C + r1 * Math.cos(a))} y1={round(C + r1 * Math.sin(a))}
            x2={round(C + r2 * Math.cos(a))} y2={round(C + r2 * Math.sin(a))}
            stroke="rgb(var(--line))" strokeWidth={k % 6 === 0 ? 1.5 : 1} />;
        })}
        <circle cx={C} cy={C} r={R} fill="none" stroke="rgb(var(--line))" strokeWidth={6} opacity={0.6} />
        <circle cx={C} cy={C} r={R} fill="none" stroke="url(#loop-grad)" strokeWidth={2.5} strokeLinecap="round"
          transform={`rotate(-90 ${C} ${C})`} className="loop-draw" style={{ ["--len" as string]: round(circumference) }} />
        {/* Closing arrow just before Detect: verified recovery feeds the next watch. */}
        <path d={`M${C - 9},${C - R - 6} L${C - 3},${C - R} L${C - 9},${C - R + 6}`} fill="none" stroke="rgb(var(--accent2))"
          strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="fade-in" style={{ animationDelay: "1.2s" }} />

        {LOOP_STATIONS.map((label, i) => {
          const p = at(i, R);
          const l = at(i, LABEL_R);
          const on = busy.has(i);
          const anchor = Math.abs(l.x - C) < 8 ? "middle" : l.x > C ? "start" : "end";
          return (
            <g key={label} className="station-in" style={{ animationDelay: `${0.25 + i * 0.12}s` }}>
              <circle cx={p.x} cy={p.y} r={on ? 7 : 5} fill={on ? "rgb(var(--accent))" : "rgb(var(--panel))"}
                stroke={on ? "rgb(var(--accent))" : "rgb(var(--muted))"} strokeWidth={1.8}
                style={{ transition: "r 300ms ease, fill 300ms ease" }} />
              <text x={l.x} y={l.y + 4} textAnchor={anchor} fontSize={12.5} fontWeight={on ? 700 : 500}
                fill={on ? "rgb(var(--ink))" : "rgb(var(--ink2))"} style={{ transition: "fill 300ms ease" }}>{label}</text>
            </g>
          );
        })}

        {demo && (() => {
          const p = at(demoStation, R);
          return (
            <g style={{ transform: `translate(${p.x}px, ${p.y}px)`, transition: "transform 900ms cubic-bezier(0.65,0,0.35,1)" }}>
              <circle r={7} fill="none" stroke="rgb(var(--serious))" strokeWidth={2} className="ping-ring" />
              <circle r={5} fill="rgb(var(--serious))" stroke="rgb(var(--bg))" strokeWidth={2} />
            </g>
          );
        })()}

        {placed.map(({ i, station }, k) => {
          // Several incidents at one station stack inward along the radius.
          const depth = placed.slice(0, k).filter((q) => q.station === station).length;
          const p = at(station, R - depth * 16);
          const color = SEV_COLOR[i.severity] ?? "var(--warning)";
          return (
            <Link key={i.id} href={`/incidents/${i.id}`} aria-label={`${i.id}: ${i.title}, at ${LOOP_STATIONS[station]}`}>
              <g transform={`translate(${p.x},${p.y})`} className="fade-in" style={{ animationDelay: "1.1s", cursor: "pointer" }}>
                <circle r={7} fill="none" stroke={`rgb(${color})`} strokeWidth={2} className="ping-ring" />
                <circle r={6} fill={`rgb(${color})`} stroke="rgb(var(--bg))" strokeWidth={2.5} />
                <title>{`${i.severity} ${i.title} (${LOOP_STATIONS[station]})`}</title>
              </g>
            </Link>
          );
        })}
      </svg>
      {children && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center" style={{ paddingTop: "2%" }}>
          <div className="pointer-events-auto text-center">{children}</div>
        </div>
      )}
    </div>
  );
}
