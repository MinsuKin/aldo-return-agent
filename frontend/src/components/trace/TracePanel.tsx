import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useStickToEnd } from "../../hooks/useStickToEnd";
import { ACTOR_ORDER } from "../../lib/actors";
import type { TraceItem } from "../../lib/timeline";
import { Icon } from "../Icon";
import { ActorChip } from "./ActorChip";
import { EventRow, RunRow } from "./TraceRows";

interface Props {
  items: TraceItem[];
  signedIn: boolean;
  /** A run is active or a write-back is owed: the feed is polling quickly. */
  live: boolean;
}

const NEAR_BOTTOM_PX = 60;

/**
 * The live agent-to-agent trace: public events only, newest at the bottom, auto-scrolling while the
 * reader is at the bottom and a "Jump to latest" button when they have scrolled up.
 */
export function TracePanel({ items, signedIn, live }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLOListElement>(null);
  const { stick, scrollTo, onScroll } = useStickToEnd(scrollRef, NEAR_BOTTOM_PX);
  const lastToggle = useRef(0);
  const [atBottom, setAtBottom] = useState(true);

  const t0 = useMemo(() => {
    const times = items.map((i) => i.tsMs).filter((t) => Number.isFinite(t));
    return times.length ? Math.min(...times) : null;
  }, [items]);

  // Follow new events while the reader is at the bottom (but not right after they opened a run's tool calls).
  const follow = useCallback(() => {
    if (stick.current && Date.now() - lastToggle.current > 600) scrollTo();
  }, [stick, scrollTo]);
  useLayoutEffect(follow, [items.length, follow]);
  useLayoutEffect(() => {
    const inner = innerRef.current;
    if (!inner) return;
    const observer = new ResizeObserver(follow);
    observer.observe(inner);
    return () => observer.disconnect();
  }, [follow]);

  const handleScroll = () => {
    const following = onScroll();
    if (following !== null) setAtBottom(following);
  };

  const jump = () => {
    stick.current = true;
    setAtBottom(true);
    scrollTo();
  };

  const empty = items.length === 0;
  // Idle / Waiting / Live / Done, as the component already computes.
  const status = live ? "Live" : signedIn ? (empty ? "Idle" : "Done") : "Waiting";
  const statusState = live ? "running" : status === "Done" ? "done" : "idle";

  return (
    <section className="trace-panel panel" aria-labelledby="trace-title" data-testid="trace-panel">
      <header className="panel-bar">
        <Icon name="hierarchy" size={16} />
        <h2 id="trace-title" className="pb-title">
          A2A trace
        </h2>
        <span className="pb-progress" data-state={statusState} data-testid="live-pill">
          <span className="pb-phase">{status}</span>
        </span>
      </header>

      <div className="trace-legend">
        <ul className="actor-legend" aria-label="Who sends each event">
          {ACTOR_ORDER.map((a) => (
            <li key={a}>
              <ActorChip actor={a} />
            </li>
          ))}
        </ul>
        <p className="trace-desc">Agent-to-agent events as they happen. Newest at the bottom.</p>
        <p className="trace-note" data-testid="trace-note">
          Trace: public fields only, your personal agent&apos;s private memory never appears here.
        </p>
      </div>

      <div
        ref={scrollRef}
        className="trace"
        role="log"
        aria-live="off"
        aria-label="Agent-to-agent event timeline"
        tabIndex={0}
        onScroll={handleScroll}
        data-testid="a2a-trace"
      >
        {empty && (
          <p className="trace-empty" data-testid="trace-empty">
            {signedIn
              ? "No events yet. Say something to your personal agent and its steps appear here, live."
              : "Pick a customer on the phone. The agent-to-agent trace appears here, live."}
          </p>
        )}
        <ol ref={innerRef} className="timeline">
          {items.map((item) =>
            item.kind === "run" ? (
              <RunRow key={item.key} item={item} t0={t0} onToggle={() => (lastToggle.current = Date.now())} />
            ) : (
              <EventRow key={item.key} item={item} t0={t0} />
            ),
          )}
        </ol>
      </div>

      {!atBottom && !empty && (
        <button type="button" className="jump-latest" onClick={jump} data-testid="jump-latest">
          <Icon name="chevron-down" size={16} /> Jump to latest
        </button>
      )}
    </section>
  );
}
