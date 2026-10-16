import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { RunEvent } from "../api/types";
import { transactionKind } from "../lib/normalize";
import { pick, str } from "../lib/util";

/** Whether ALDO's size update to the personal agent is still owed after an exchange. */
export type WritebackState = "idle" | "waiting" | "late";

/** After an exchange is created, ALDO's size update to the personal agent is delivered when memory is read. */
const WINDOW_MS = 15_000;
const RETRY_MS = 2_500;

/**
 * The API flushes the write-back outbox before it reads the personal agent's memory, so reading memory is what
 * delivers the update. As soon as the A2A stream shows an exchange without its memory_updated event,
 * read memory right away and retry for at most WINDOW_MS, then say so and offer "Check again".
 */
export function useWriteback(events: RunEvent[]): { writeback: WritebackState; checkAgain: () => void; memoryVersion: number } {
  const qc = useQueryClient();
  const [waitingSince, setWaitingSince] = useState<number | null>(null);
  const [late, setLate] = useState(false);

  const memoryVersion = useMemo(() => events.filter((e) => e.type === "memory_updated").length, [events]);

  const owed = useMemo(() => {
    const exchanges = events.filter(
      (e) =>
        e.type === "transaction_created" &&
        transactionKind(str(pick(e.payload, ["transaction_id"])) ?? "", str(pick(e.payload, ["option_type"]))) === "exchange",
    ).length;
    const delivered = events.filter((e) => e.type === "memory_updated" && str(pick(e.payload, ["outcome"])) !== "exchange_completed_kept").length;
    return exchanges - delivered;
  }, [events]);

  useEffect(() => {
    if (owed > 0) setWaitingSince((prev) => prev ?? Date.now());
    else {
      setWaitingSince(null);
      setLate(false);
    }
  }, [owed]);

  useEffect(() => {
    if (waitingSince === null || late) return;
    void qc.refetchQueries({ queryKey: ["pa-memory"] });
    const id = window.setInterval(() => {
      if (Date.now() - waitingSince >= WINDOW_MS) {
        setLate(true);
        return;
      }
      void qc.refetchQueries({ queryKey: ["pa-memory"] });
    }, RETRY_MS);
    return () => window.clearInterval(id);
  }, [waitingSince, late, qc]);

  useEffect(() => {
    if (memoryVersion > 0) void qc.invalidateQueries({ queryKey: ["pa-memory"] });
  }, [memoryVersion, qc]);

  const checkAgain = useCallback(() => {
    setLate(false);
    setWaitingSince(Date.now());
  }, []);

  return { writeback: waitingSince === null ? "idle" : late ? "late" : "waiting", checkAgain, memoryVersion };
}
