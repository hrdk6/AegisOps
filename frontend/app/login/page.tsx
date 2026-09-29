"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { login } from "@/lib/api";
import { AegisMark } from "@/components/AppShell";
import { LoopDial } from "@/components/LoopDial";
import { Button, ErrorNote } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative isolate grid min-h-screen overflow-hidden lg:grid-cols-[1.15fr_1fr]">
      <div aria-hidden className="state-glow -z-10" />
      <section className="flex flex-col justify-between px-6 py-8 sm:px-12 lg:py-12">
        <div className="flex items-center gap-2.5">
          <AegisMark size={32} />
          <span className="font-wide text-xl font-extrabold">AegisOps</span>
        </div>
        <div className="my-10 grid items-center gap-8 xl:grid-cols-[minmax(0,340px)_1fr]">
          <LoopDial demo className="mx-auto w-full max-w-[340px] xl:mx-0" />
          <div className="max-w-md">
            <h1 className="font-wide text-2xl font-extrabold sm:text-3xl">Incidents handled inside the lines you draw.</h1>
            <p className="mt-4 text-md leading-relaxed text-ink2">
              AegisOps detects SLO breaches, finds the root cause with cited evidence, tests the fix in a sandbox and
              only acts when your policy allows it. Every step is recorded, and anything that doesn&apos;t help is rolled back.
            </p>
          </div>
        </div>
        <p className="hidden text-xs text-muted lg:block">Reliability control plane for Kubernetes</p>
      </section>

      <section className="flex items-center justify-center border-line/70 px-6 pb-12 lg:border-l lg:bg-panel/40 lg:pb-0 lg:backdrop-blur">
        <div className="w-full max-w-sm">
          <h2 className="font-display text-xl font-bold">Sign in</h2>
          <p className="mt-1.5 text-ink2">Accounts and roles are provisioned by your operator.</p>
          <form onSubmit={submit} className="mt-6 space-y-4">
            <label className="block text-sm">
              <span className="text-ink2">Username</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required
                className="mt-1.5 w-full rounded-lg border border-line bg-bg/70 px-3 py-2.5 text-ink outline-none transition-[border-color,box-shadow] focus:border-accent focus:shadow-[0_0_0_3px_rgb(var(--accent)/0.18)]" />
            </label>
            <label className="block text-sm">
              <span className="text-ink2">Password</span>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
                required className="mt-1.5 w-full rounded-lg border border-line bg-bg/70 px-3 py-2.5 text-ink outline-none transition-[border-color,box-shadow] focus:border-accent focus:shadow-[0_0_0_3px_rgb(var(--accent)/0.18)]" />
            </label>
            {error ? <ErrorNote error={error} /> : null}
            <Button type="submit" variant="primary" busy={busy} className="w-full py-2.5">Sign in</Button>
          </form>
          <p className="mt-5 text-xs text-muted">
            Local demo credentials are printed by <code className="font-mono text-ink2">python scripts/devctl.py credentials</code>.
          </p>
        </div>
      </section>
    </main>
  );
}
