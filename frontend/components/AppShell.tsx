"use client";

import clsx from "clsx";
import {
  Activity, ClipboardCheck, FlaskConical, Gauge, LayoutDashboard, LogOut, Moon, Network, ScrollText, ShieldCheck, Siren, Sun,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import useSWR from "swr";
import { fetcher, getSession, setSession, type Session } from "@/lib/api";
import { useEventStream } from "@/lib/events";
import { Chip } from "./ui";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/incidents", label: "Incidents", icon: Siren },
  { href: "/services", label: "Services", icon: Network },
  { href: "/automation", label: "Automation & policy", icon: ShieldCheck },
  { href: "/evaluation", label: "Evaluation", icon: Gauge },
  { href: "/demo", label: "Demo scenarios", icon: FlaskConical },
  { href: "/audit", label: "Audit trail", icon: ScrollText },
  { href: "/system", label: "System health", icon: Activity },
];

type Overview = { automation: { mode: string; breaker?: { state?: string } }; pending_approvals: number };

export function AegisMark({ size = 28 }: { size?: number }) {
  // Shield outline holding a closed loop with one arrowhead: bounded autonomy.
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden>
      <defs>
        <linearGradient id="aegis-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="rgb(var(--accent2))" />
          <stop offset="1" stopColor="rgb(var(--accent))" />
        </linearGradient>
      </defs>
      <path d="M14 2.2 L24.5 6.3 V13.2 C24.5 19.6 20 24 14 25.9 C8 24 3.5 19.6 3.5 13.2 V6.3 Z"
        fill="rgb(var(--accent) / 0.1)" stroke="url(#aegis-mark)" strokeWidth="1.9" strokeLinejoin="round" />
      <path d="M9.2 14.6 a4.9 4.9 0 1 0 1.8 -4.1" fill="none" stroke="rgb(var(--ink))" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M10 8.3 L11.2 10.9 L8.4 11.3" fill="none" stroke="rgb(var(--ink))" strokeWidth="1.8" strokeLinecap="round"
        strokeLinejoin="round" />
      <circle cx="14" cy="14.3" r="1.6" fill="rgb(var(--accent))" />
    </svg>
  );
}

const MODE_TONE = { autonomous: "accent", supervised: "warning", observe: "neutral" } as const;

export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const [session, setSess] = useState<Session | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const stream = useEventStream();
  const { data: overview } = useSWR<Overview>(session ? "/api/v1/overview" : null, fetcher, { refreshInterval: 30000 });

  useEffect(() => {
    const s = getSession();
    if (!s) router.replace("/login");
    else setSess(s);
    setTheme((document.documentElement.dataset.theme as "dark" | "light") ?? "dark");
  }, [router]);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("aegis-theme", next); } catch { /* per-viewer convenience only */ }
    setTheme(next);
  }

  if (!session) return null;
  const mode = overview?.automation.mode ?? "unknown";
  const breaker = overview?.automation.breaker?.state;
  const pending = overview?.pending_approvals ?? 0;
  const activeIdx = NAV.findIndex(({ href }) => (href === "/" ? path === "/" : path.startsWith(href)));

  return (
    <div className="flex min-h-screen">
      <nav aria-label="Primary"
        className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line/70 bg-panel/80 backdrop-blur md:flex">
        <Link href="/" className="flex items-center gap-2.5 px-5 pb-5 pt-5">
          <AegisMark />
          <span className="font-wide text-lg font-extrabold">AegisOps</span>
        </Link>
        <ul className="relative flex-1 space-y-0.5 px-3">
          {activeIdx >= 0 && (
            // One indicator that slides between items, so a click shows where you went.
            <span aria-hidden className="absolute inset-x-3 top-0 h-9 rounded-lg border border-accent/25 bg-accent/[0.09] transition-transform duration-300 ease-out"
              style={{ transform: `translateY(${activeIdx * 38}px)` }}>
              <span className="absolute -left-3 top-2 h-5 w-[3px] rounded-r bg-gradient-to-b from-accent2 to-accent" />
            </span>
          )}
          {NAV.map(({ href, label, icon: Icon }, i) => {
            const active = i === activeIdx;
            return (
              <li key={href}>
                <Link href={href} aria-current={active ? "page" : undefined}
                  className={clsx("relative flex h-9 items-center gap-2.5 rounded-lg px-3 text-sm transition-colors",
                    active ? "font-medium text-ink" : "text-ink2 hover:bg-raised/60 hover:text-ink")}>
                  <Icon size={16} aria-hidden className={clsx("transition-colors", active ? "text-accent" : "text-muted")} />
                  {label}
                  {href === "/automation" && pending > 0 && (
                    <span className="ml-auto rounded-full bg-serious px-1.5 text-xs font-semibold text-bg">{pending}</span>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="m-3 flex items-center gap-2.5 rounded-lg border border-line/70 bg-raised/50 p-2.5">
          <span aria-hidden className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-accent2 to-accent font-display text-sm font-bold text-accentink">
            {session.user.username.slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0 text-xs">
            <div className="truncate text-sm text-ink">{session.user.username}</div>
            <div className="text-muted">{session.user.role}</div>
          </div>
        </div>
      </nav>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line/70 bg-bg/80 px-4 py-2.5 backdrop-blur-md md:px-8">
          <Link href="/" className="flex items-center gap-2 md:hidden"><AegisMark /></Link>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted">Automation</span>
            <Chip tone={MODE_TONE[mode as keyof typeof MODE_TONE] ?? "neutral"}>{mode}</Chip>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted">Circuit breaker</span>
            <Chip tone={breaker === "open" ? "critical" : breaker === "closed" ? "good" : "neutral"}>{breaker ?? "unknown"}</Chip>
          </div>
          <Link href="/automation#approvals" className="flex items-center gap-2 text-sm">
            <ClipboardCheck size={15} aria-hidden className="text-muted" />
            <span className={clsx(pending ? "font-semibold text-serious" : "text-ink2")}>
              {pending} pending approval{pending === 1 ? "" : "s"}
            </span>
          </Link>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="flex items-center gap-2 rounded-full border border-line/70 bg-panel/60 px-2.5 py-0.5 text-ink2" aria-live="polite">
              <span className="relative flex h-2 w-2">
                {stream === "live" && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-good opacity-60" />}
                <span className={clsx("relative h-2 w-2 rounded-full", stream === "live" ? "bg-good" : stream === "connecting" ? "bg-warning" : "bg-critical")} />
              </span>
              {stream === "live" ? "Live" : stream === "connecting" ? "Connecting" : "Stream offline"}
            </span>
            <button onClick={toggleTheme} className="rounded-lg p-1.5 text-ink2 transition-colors hover:bg-raised hover:text-ink" aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button onClick={() => { setSession(null); router.replace("/login"); }}
              className="flex items-center gap-1 rounded-lg p-1.5 text-ink2 transition-colors hover:bg-raised hover:text-ink" aria-label="Sign out">
              <LogOut size={16} />
            </button>
          </div>
          <nav aria-label="Primary mobile" className="flex w-full gap-3 overflow-x-auto text-sm md:hidden">
            {NAV.map(({ href, label }) => (
              <Link key={href} href={href} className={clsx("whitespace-nowrap pb-1", NAV[activeIdx]?.href === href ? "border-b-2 border-accent text-ink" : "text-ink2")}>{label}</Link>
            ))}
          </nav>
        </header>
        <main className="min-w-0 flex-1 overflow-x-clip px-4 py-6 md:px-8">{children}</main>
      </div>
    </div>
  );
}
