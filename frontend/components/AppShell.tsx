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

function AegisMark() {
  // Shield outline with a closed loop inside: bounded autonomy.
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden>
      <path d="M13 2 L23 6 V12.5 C23 18.5 18.8 22.6 13 24.5 C7.2 22.6 3 18.5 3 12.5 V6 Z" fill="none"
        stroke="rgb(var(--accent))" strokeWidth="1.8" />
      <path d="M8.5 13.5 a4.5 4.5 0 1 0 1.6 -3.7" fill="none" stroke="rgb(var(--ink))" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M9.2 7.6 L10.3 10 L7.7 10.3" fill="none" stroke="rgb(var(--ink))" strokeWidth="1.6" strokeLinecap="round"
        strokeLinejoin="round" />
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

  return (
    <div className="flex min-h-screen">
      <nav aria-label="Primary" className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-line bg-panel md:flex">
        <Link href="/" className="flex items-center gap-2.5 px-4 py-4">
          <AegisMark />
          <span className="font-display text-lg font-bold tracking-wide">AegisOps</span>
        </Link>
        <ul className="mt-2 flex-1 space-y-0.5 px-2">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? path === "/" : path.startsWith(href);
            return (
              <li key={href}>
                <Link href={href} aria-current={active ? "page" : undefined}
                  className={clsx("flex items-center gap-2.5 rounded px-3 py-2 text-sm",
                    active ? "bg-raised text-ink" : "text-ink2 hover:bg-raised/60 hover:text-ink")}>
                  <Icon size={16} aria-hidden className={active ? "text-accent" : ""} />
                  {label}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="border-t border-line p-3 text-xs text-muted">
          <div className="text-ink2">{session.user.username}</div>
          <div>{session.user.role}</div>
        </div>
      </nav>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-bg/95 px-4 py-2 backdrop-blur md:px-6">
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
            <span className={clsx(overview?.pending_approvals ? "font-semibold text-serious" : "text-ink2")}>
              {overview?.pending_approvals ?? 0} pending approval{overview?.pending_approvals === 1 ? "" : "s"}
            </span>
          </Link>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="flex items-center gap-1.5 text-ink2" aria-live="polite">
              <span className={clsx("h-2 w-2 rounded-full", stream === "live" ? "bg-good" : stream === "connecting" ? "bg-warning" : "bg-critical")} />
              {stream === "live" ? "Live" : stream === "connecting" ? "Connecting" : "Stream offline"}
            </span>
            <button onClick={toggleTheme} className="rounded p-1.5 text-ink2 hover:text-ink" aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button onClick={() => { setSession(null); router.replace("/login"); }}
              className="flex items-center gap-1 rounded p-1.5 text-ink2 hover:text-ink" aria-label="Sign out">
              <LogOut size={16} />
            </button>
          </div>
          <nav aria-label="Primary mobile" className="flex w-full gap-3 overflow-x-auto text-sm md:hidden">
            {NAV.map(({ href, label }) => (
              <Link key={href} href={href} className={clsx("whitespace-nowrap", path === href ? "text-accent" : "text-ink2")}>{label}</Link>
            ))}
          </nav>
        </header>
        <main className="min-w-0 flex-1 px-4 py-5 md:px-6">{children}</main>
      </div>
    </div>
  );
}
