"use client";

// Session token lives in sessionStorage (cleared when the tab closes). The API
// is same-origin via the /api proxy, so the token is never sent to another host.
const TOKEN_KEY = "aegis-session";

export type User = { username: string; role: string; permissions: string[] };
export type Session = { token: string; expiresAt: number; user: User };

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (s.expiresAt * 1000 < Date.now()) {
      sessionStorage.removeItem(TOKEN_KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

export function setSession(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session lasts for this page only */
  }
}

export function can(permission: string): boolean {
  return getSession()?.user.permissions.includes(permission) ?? false;
}

export async function api<T = unknown>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const session = getSession();
  const headers = new Headers(init?.headers);
  if (session) headers.set("Authorization", `Bearer ${session.token}`);
  let body = init?.body;
  if (init?.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const resp = await fetch(path, { ...init, headers, body, cache: "no-store" });
  if (resp.status === 401 && typeof window !== "undefined" && !path.endsWith("/auth/login")) {
    setSession(null);
    window.location.href = "/login";
  }
  const text = await resp.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!resp.ok) {
    const err = (data && typeof data === "object" && "error" in data ? (data as { error: { code: string; message: string } }).error : null);
    throw new ApiError(resp.status, err?.code ?? "error", err?.message ?? `Request failed (${resp.status})`);
  }
  return data as T;
}

export const fetcher = <T,>(path: string) => api<T>(path);

export async function login(username: string, password: string): Promise<Session> {
  const body = await api<{ access_token: string; expires_at: number; user: User }>("/api/v1/auth/login", {
    method: "POST",
    json: { username, password },
  });
  const s = { token: body.access_token, expiresAt: body.expires_at, user: body.user };
  setSession(s);
  return s;
}
