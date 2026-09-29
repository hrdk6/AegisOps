"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { login } from "@/lib/api";
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
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="font-display text-2xl font-bold tracking-wide">AegisOps</h1>
        <p className="mt-2 text-ink2">
          Sign in to the reliability control plane. Accounts and roles are provisioned by your operator.
        </p>
        <form onSubmit={submit} className="mt-6 space-y-4 rounded-lg border border-line bg-panel p-5">
          <label className="block text-sm">
            <span className="text-ink2">Username</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required
              className="mt-1 w-full rounded border border-line bg-bg px-3 py-2 text-ink outline-none focus:border-accent" />
          </label>
          <label className="block text-sm">
            <span className="text-ink2">Password</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
              required className="mt-1 w-full rounded border border-line bg-bg px-3 py-2 text-ink outline-none focus:border-accent" />
          </label>
          {error ? <ErrorNote error={error} /> : null}
          <Button type="submit" variant="primary" busy={busy} className="w-full">Sign in</Button>
        </form>
        <p className="mt-4 text-xs text-muted">
          Local demo credentials are printed by <code className="font-mono">python scripts/devctl.py credentials</code>.
        </p>
      </div>
    </main>
  );
}
