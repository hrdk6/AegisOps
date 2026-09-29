"use client";

import { useEffect, useRef, useState } from "react";
import { mutate } from "swr";
import { getSession } from "./api";

export type StreamEvent = {
  id: number;
  incident_id: string | null;
  ts: string;
  type: string;
  message: string;
  actor: string;
  data: Record<string, unknown>;
};

type Listener = (e: StreamEvent) => void;
const listeners = new Set<Listener>();
let started = false;
let status: "connecting" | "live" | "offline" = "connecting";
const statusListeners = new Set<(s: typeof status) => void>();

function setStatus(s: typeof status) {
  status = s;
  statusListeners.forEach((l) => l(s));
}

function revalidate(e: StreamEvent) {
  // Keep every open view fresh without polling: refetch the resources an event touches.
  mutate((key) => typeof key === "string" && (key.startsWith("/api/v1/overview") || key.startsWith("/api/v1/incidents?")));
  if (e.incident_id) {
    mutate((key) => typeof key === "string" && key.startsWith(`/api/v1/incidents/${e.incident_id}`));
  }
  if (e.type.startsWith("approval") || e.type.startsWith("action") || e.type === "policy_decision") {
    mutate((key) => typeof key === "string" && (key.startsWith("/api/v1/approvals") || key.startsWith("/api/v1/actions")
      || key.startsWith("/api/v1/policy/decisions")));
  }
}

async function run() {
  let lastId = 0;
  let backoff = 1000;
  for (;;) {
    const session = getSession();
    if (!session) {
      setStatus("offline");
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }
    try {
      setStatus("connecting");
      const resp = await fetch(`/api/v1/events/stream${lastId ? `?after=${lastId}` : ""}`, {
        headers: { Authorization: `Bearer ${session.token}`, Accept: "text/event-stream" },
        cache: "no-store",
      });
      if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);
      setStatus("live");
      backoff = 1000;
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const data = chunk.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          if (!data) continue;
          try {
            const ev = JSON.parse(data) as StreamEvent;
            lastId = Math.max(lastId, ev.id);
            listeners.forEach((l) => l(ev));
            revalidate(ev);
          } catch {
            /* ignore malformed frames */
          }
        }
      }
      throw new Error("stream closed");
    } catch {
      setStatus("offline");
      await new Promise((r) => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 15000);
    }
  }
}

export function useEventStream(onEvent?: Listener) {
  const ref = useRef(onEvent);
  ref.current = onEvent;
  const [s, setS] = useState(status);
  useEffect(() => {
    if (!started) {
      started = true;
      void run();
    }
    const l: Listener = (e) => ref.current?.(e);
    listeners.add(l);
    statusListeners.add(setS);
    return () => {
      listeners.delete(l);
      statusListeners.delete(setS);
    };
  }, []);
  return s;
}
