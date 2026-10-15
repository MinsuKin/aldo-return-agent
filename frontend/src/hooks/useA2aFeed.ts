import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RunEvent } from "../api/types";
import { POLL_INTERVAL_MS } from "../config";

const IDLE_POLL_MS = 4000;
const COOLDOWN_MS = 8000;
/** Every Nth request re-reads from after=0, so a demo reset in another tab can never hide events. */
const FULL_REFRESH_EVERY = 10;

function merge(prev: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const bySeq = new Map(prev.map((e) => [e.seq, e]));
  let changed = false;
  for (const e of incoming) {
    if (!bySeq.has(e.seq)) {
      bySeq.set(e.seq, e);
      changed = true;
    }
  }
  return changed ? [...bySeq.values()].sort((a, b) => a.seq - b.seq) : prev;
}

/**
 * The long-lived per-owner agent-to-agent stream (GET /a2a/events?after=n). Polls every
 * POLL_INTERVAL_MS while either agent is running and for a short cooldown afterwards (a
 * write-back can land just after a run ends), then slowly while idle.
 */
export function useA2aFeed(busy: boolean): RunEvent[] {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const afterRef = useRef(0);
  const countRef = useRef(0);
  const busyRef = useRef(busy);
  const cooldownUntil = useRef(0);

  useEffect(() => {
    busyRef.current = busy;
    if (!busy) cooldownUntil.current = Date.now() + COOLDOWN_MS;
  }, [busy]);

  const query = useQuery({
    queryKey: ["a2a-events"],
    queryFn: async ({ signal }) => {
      countRef.current += 1;
      const full = countRef.current % FULL_REFRESH_EVERY === 0;
      const data = await api.a2aEvents(full ? 0 : afterRef.current, signal);
      return { full, data };
    },
    refetchInterval: () => (busyRef.current || Date.now() < cooldownUntil.current ? POLL_INTERVAL_MS : IDLE_POLL_MS),
    refetchIntervalInBackground: true,
    retry: 1,
    gcTime: 0,
    staleTime: 0,
  });

  const result = query.data;
  useEffect(() => {
    if (!result) return;
    const incoming = result.data.events ?? [];
    const highest = incoming.reduce((m, e) => Math.max(m, e.seq), 0);
    const next = typeof result.data.next_after === "number" ? result.data.next_after : highest;
    if (result.full) {
      const sorted = [...incoming].sort((a, b) => a.seq - b.seq);
      setEvents((prev) => (prev.length === sorted.length && prev.at(-1)?.seq === sorted.at(-1)?.seq ? prev : sorted));
      afterRef.current = next;
    } else {
      setEvents((prev) => merge(prev, incoming));
      if (next > afterRef.current) afterRef.current = next;
    }
  }, [result]);

  return events;
}
