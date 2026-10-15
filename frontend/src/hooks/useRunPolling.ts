import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RunEvent } from "../api/types";
import { POLL_INTERVAL_MS } from "../config";
import { isTerminal } from "../lib/runs";

type OnEvents = (runId: string, events: RunEvent[], status: string) => void;

/**
 * Polls GET /runs/{id}/events?after=n every POLL_INTERVAL_MS while the run is active
 * (requirement 11.2). When the run reaches a terminal status it re-reads from after=0 once,
 * so an event that became visible out of order is never lost.
 */
export function useRunPolling(runId: string | null, onEvents: OnEvents): void {
  const afterRef = useRef(0);
  const reconciledRef = useRef<string | null>(null);

  useEffect(() => {
    afterRef.current = 0;
  }, [runId]);

  const query = useQuery({
    queryKey: ["run-events", runId],
    enabled: runId !== null,
    queryFn: ({ signal }) => api.runEvents(runId as string, afterRef.current, signal),
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : POLL_INTERVAL_MS),
    refetchIntervalInBackground: true,
    retry: 2,
    gcTime: 0,
    staleTime: 0,
  });

  const data = query.data;
  useEffect(() => {
    if (!runId || !data) return;
    const events = data.events ?? [];
    onEvents(runId, events, data.status);
    const highest = events.reduce((m, e) => Math.max(m, e.seq), 0);
    const next = typeof data.next_after === "number" ? data.next_after : highest;
    if (next > afterRef.current) afterRef.current = next;
    if (isTerminal(data.status) && reconciledRef.current !== runId) {
      reconciledRef.current = runId;
      api
        .runEvents(runId, 0)
        .then((full) => onEvents(runId, full.events ?? [], full.status))
        .catch(() => undefined);
    }
  }, [runId, data, onEvents]);
}
