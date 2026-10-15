import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useA2aFeed } from "../hooks/useA2aFeed";
import { transactionKind } from "../lib/normalize";
import { pick, str } from "../lib/util";
import { AldoPanel } from "./AldoPanel";
import { PersonalAgentPanel, type WritebackState } from "./PersonalAgentPanel";
import { ActorBadge, TraceTimeline, type TraceGroup } from "./TraceTimeline";

const ACTORS = ["personal_agent", "aldo_gateway", "aldo_agent", "system"] as const;

/** After an exchange is created, ALDO's size update to ALDO bot is delivered when memory is read. */
const WRITEBACK_WINDOW_MS = 15_000;
const WRITEBACK_RETRY_MS = 2_500;

/**
 * Three columns at 1200 px and up (ALDO bot, A2A trace, ALDO Returns Agent); stacked below.
 * The A2A feed is shared state: it drives the middle column and tells the others when to refresh.
 */
export function Workspace() {
  const qc = useQueryClient();
  const [paActive, setPaActive] = useState(false);
  const [aldoActive, setAldoActive] = useState(false);
  const [paKey, setPaKey] = useState(0);
  const [aldoKey, setAldoKey] = useState(0);
  const [waitingSince, setWaitingSince] = useState<number | null>(null);
  const [late, setLate] = useState(false);

  const { events, nudge } = useA2aFeed(paActive || aldoActive || (waitingSince !== null && !late));
  const memoryVersion = useMemo(() => events.filter((e) => e.type === "memory_updated").length, [events]);
  const confirmedCount = useMemo(() => events.filter((e) => e.type === "size_confirmed").length, [events]);
  // The newest proposal ALDO bot recorded; the purchase card is rendered from the server's record of it.
  const proposalId = useMemo(() => {
    const last = events.filter((e) => e.type === "size_recommended").at(-1);
    return last ? str(pick(last.payload, ["size_request_id"])) : undefined;
  }, [events]);

  // Write-backs ALDO still owes ALDO bot: exchanges created minus the "exchange_created" updates delivered.
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

  // The API flushes the outbox before it reads memory, so reading memory delivers the write-back.
  // Read it right away, then retry while the A2A feed is polled, for at most WRITEBACK_WINDOW_MS.
  useEffect(() => {
    if (waitingSince === null || late) return;
    void qc.refetchQueries({ queryKey: ["pa-memory"] });
    const id = window.setInterval(() => {
      if (Date.now() - waitingSince >= WRITEBACK_WINDOW_MS) {
        setLate(true);
        return;
      }
      void qc.refetchQueries({ queryKey: ["pa-memory"] });
    }, WRITEBACK_RETRY_MS);
    return () => window.clearInterval(id);
  }, [waitingSince, late, qc]);

  const writeback: WritebackState = waitingSince === null ? "idle" : late ? "late" : "waiting";
  const checkAgain = useCallback(() => {
    setLate(false);
    setWaitingSince(Date.now());
  }, []);

  // A confirmed purchase creates an order; refresh the order list as soon as it appears.
  useEffect(() => {
    if (confirmedCount > 0) void qc.invalidateQueries({ queryKey: ["orders"] });
  }, [confirmedCount, qc]);

  const groups: TraceGroup[] = useMemo(() => [{ key: "a2a", events }], [events]);
  const restartPa = useCallback(() => setPaKey((k) => k + 1), []);
  const restartAldo = useCallback(() => setAldoKey((k) => k + 1), []);

  return (
    <div className="workspace" data-testid="workspace">
      <section className="column column-pa" aria-labelledby="col-pa-title">
        <h2 id="col-pa-title" className="column-title">
          ALDO bot, your personal agent
        </h2>
        <PersonalAgentPanel
          key={paKey}
          memoryVersion={memoryVersion}
          proposalId={proposalId}
          confirmedCount={confirmedCount}
          writeback={writeback}
          onCheckAgain={checkAgain}
          onActive={setPaActive}
          onRestart={restartPa}
        />
      </section>

      <section className="column column-a2a" aria-labelledby="col-a2a-title">
        <h2 id="col-a2a-title" className="column-title">
          A2A trace
        </h2>
        <div className="card a2a-card">
          <p className="card-sub muted">
            The conversation between the two agents. Public fields only: ALDO bot&apos;s private memory never appears here.
          </p>
          <ul className="actor-legend" aria-label="Who sends each event">
            {ACTORS.map((a) => (
              <li key={a}>
                <ActorBadge actor={a} />
              </li>
            ))}
          </ul>
          <TraceTimeline
            groups={groups}
            showActors
            emptyText="No agent-to-agent events yet. Ask ALDO bot to buy a pair."
            ariaLabel="Agent-to-agent event timeline"
            testId="a2a-trace"
          />
        </div>
      </section>

      <section className="column column-aldo" aria-labelledby="col-aldo-title">
        <h2 id="col-aldo-title" className="column-title">
          ALDO Returns Agent
        </h2>
        <AldoPanel key={aldoKey} onActive={setAldoActive} onRestart={restartAldo} onNudge={nudge} />
      </section>
    </div>
  );
}
