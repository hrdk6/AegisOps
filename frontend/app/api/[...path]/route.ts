// Same-origin proxy to the AegisOps API. The browser never talks to the API host
// directly, so no CORS exposure is needed; responses (including the server-sent
// event stream) are streamed through without buffering.
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const API = process.env.AEGIS_API_INTERNAL_URL ?? "http://localhost:8000";
const FORWARD_REQUEST = ["authorization", "content-type", "last-event-id", "x-request-id", "accept"];
const FORWARD_RESPONSE = ["content-type", "x-request-id", "cache-control"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  if (path.some((p) => p === ".." || p.includes("\\"))) {
    return new Response(JSON.stringify({ error: { code: "bad_request", message: "invalid path" } }), { status: 400 });
  }
  const target = `${API}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of FORWARD_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.text() : undefined,
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return new Response(
      JSON.stringify({ error: { code: "api_unreachable", message: "The AegisOps API is not reachable from the UI server." } }),
      { status: 502, headers: { "content-type": "application/json" } },
    );
  }
  const out = new Headers();
  for (const h of FORWARD_RESPONSE) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  if (out.get("content-type")?.includes("text/event-stream")) {
    out.set("x-accel-buffering", "no");
    out.set("cache-control", "no-cache, no-transform");
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
