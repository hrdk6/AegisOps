"use client";

import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleHelp, Loader2, OctagonX, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";

export function Panel({ title, action, children, className, dense }: {
  title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; dense?: boolean;
}) {
  return (
    <section className={clsx("rounded-lg border border-line bg-panel", className)}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="font-display text-md font-semibold tracking-wide text-ink">{title}</h2>
          {action}
        </header>
      )}
      <div className={dense ? "" : "p-4"}>{children}</div>
    </section>
  );
}

type Tone = "good" | "warning" | "serious" | "critical" | "neutral" | "accent";

const TONE: Record<Tone, string> = {
  good: "text-good border-good/40 bg-good/10",
  warning: "text-warning border-warning/40 bg-warning/10",
  serious: "text-serious border-serious/40 bg-serious/10",
  critical: "text-critical border-critical/50 bg-critical/10",
  neutral: "text-ink2 border-line bg-raised",
  accent: "text-accent border-accent/40 bg-accent/10",
};

const ICON: Record<Tone, typeof CheckCircle2> = {
  good: CheckCircle2, warning: AlertTriangle, serious: ShieldAlert, critical: OctagonX, neutral: CircleHelp,
  accent: CheckCircle2,
};

export function Chip({ tone = "neutral", icon = true, children, className, title }: {
  tone?: Tone; icon?: boolean; children: ReactNode; className?: string; title?: string;
}) {
  const Icon = ICON[tone];
  return (
    <span title={title} className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-px text-xs font-medium",
      TONE[tone], className)}>
      {icon && <Icon aria-hidden size={12} strokeWidth={2.4} />}
      {children}
    </span>
  );
}

const SERVICE_TONE: Record<string, Tone> = { healthy: "good", degraded: "warning", down: "critical", unknown: "neutral" };
export function ServiceStatus({ status }: { status: string }) {
  return <Chip tone={SERVICE_TONE[status] ?? "neutral"}>{status}</Chip>;
}

const INCIDENT_TONE: Record<string, Tone> = {
  DETECTED: "serious", INVESTIGATING: "warning", DIAGNOSED: "warning", AWAITING_APPROVAL: "serious", EXECUTING: "accent",
  VERIFYING: "accent", RESOLVED: "good", ROLLED_BACK: "serious", ESCALATED: "critical",
};
const INCIDENT_LABEL: Record<string, string> = {
  DETECTED: "Detected", INVESTIGATING: "Investigating", DIAGNOSED: "Diagnosed", AWAITING_APPROVAL: "Awaiting approval",
  EXECUTING: "Executing", VERIFYING: "Verifying", RESOLVED: "Resolved", ROLLED_BACK: "Rolled back", ESCALATED: "Escalated",
};
export function IncidentStatus({ status }: { status: string }) {
  return <Chip tone={INCIDENT_TONE[status] ?? "neutral"}>{INCIDENT_LABEL[status] ?? status}</Chip>;
}

const SEV_TONE: Record<string, Tone> = { SEV1: "critical", SEV2: "serious", SEV3: "warning" };
export function Severity({ severity }: { severity: string }) {
  return <Chip tone={SEV_TONE[severity] ?? "neutral"} icon={false} className="font-display font-semibold">{severity}</Chip>;
}

const RISK_TONE: Record<string, Tone> = { LOW: "good", MEDIUM: "warning", HIGH: "serious", CRITICAL: "critical" };
export function Risk({ level }: { level?: string | null }) {
  if (!level) return <span className="text-muted">–</span>;
  return <Chip tone={RISK_TONE[level] ?? "neutral"}>{level.charAt(0) + level.slice(1).toLowerCase()} risk</Chip>;
}

const PHASE_TONE: Record<string, Tone> = {
  Pending: "neutral", AwaitingApproval: "serious", Approved: "accent", Executing: "accent", Succeeded: "good",
  Failed: "critical", Denied: "critical", Rejected: "critical", Expired: "neutral", RolledBack: "serious",
};
export function Phase({ phase }: { phase: string }) {
  return <Chip tone={PHASE_TONE[phase] ?? "neutral"}>{phase.replace(/([a-z])([A-Z])/g, "$1 $2")}</Chip>;
}

export function Button({ children, onClick, variant = "secondary", disabled, busy, type = "button", className }: {
  children: ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "danger" | "ghost"; disabled?: boolean;
  busy?: boolean; type?: "button" | "submit"; className?: string;
}) {
  const styles = {
    primary: "bg-accent text-accentink hover:brightness-110 border-transparent",
    secondary: "bg-raised text-ink border-line hover:border-ink2/50",
    danger: "bg-critical/15 text-critical border-critical/50 hover:bg-critical/25",
    ghost: "bg-transparent text-ink2 border-transparent hover:text-ink",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled || busy}
      className={clsx("inline-flex items-center justify-center gap-1.5 rounded border px-3 py-1.5 text-sm font-medium",
        "disabled:cursor-not-allowed disabled:opacity-50", styles, className)}>
      {busy && <Loader2 size={14} className="animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted">{children}</p>;
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="rounded border border-critical/40 bg-critical/10 px-3 py-2 text-sm text-ink">
      <OctagonX size={14} className="mr-1.5 inline text-critical" aria-hidden />
      {msg}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-muted" role="status">
      <Loader2 size={14} className="animate-spin" aria-hidden /> {label}…
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-ink">{children}</dd>
    </div>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx("font-mono text-[12.5px]", className)}>{children}</span>;
}

export function Bar({ value, tone = "accent", label }: { value: number; tone?: Tone; label?: string }) {
  const color = { good: "bg-good", warning: "bg-warning", serious: "bg-serious", critical: "bg-critical", neutral: "bg-muted",
    accent: "bg-accent" }[tone];
  return (
    <div className="flex items-center gap-2" aria-label={label}>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-raised">
        <div className={clsx("h-full rounded-full", color)} style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }} />
      </div>
      <span className="tabular w-10 text-right text-xs text-ink2">{Math.round(value * 100)}%</span>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: {
  tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (t: T) => void;
}) {
  return (
    <div role="tablist" className="flex gap-1 border-b border-line px-2">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}
          className={clsx("-mb-px border-b-2 px-3 py-2 text-sm",
            value === t.id ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink2")}>
          {t.label}
          {t.count !== undefined && <span className="ml-1.5 text-xs text-muted">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}
