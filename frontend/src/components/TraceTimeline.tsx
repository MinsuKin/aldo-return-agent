import { useEffect, useMemo, useRef } from "react";
import type { RunEvent } from "../api/types";
import { ACTOR_TITLE, actorLabel } from "../lib/actors";
import { buildTraceRows, type RunMetrics, type TraceRow } from "../lib/events";
import { formatInt, formatLatency, formatSeconds } from "../lib/format";
import { ProvenanceBadge } from "./Badge";
import { Icon, type IconName } from "./Icon";
import { codeHint } from "../lib/errors";
import { ErrorCodeNote } from "./ResultPanels";

export interface TraceGroup {
  key: string;
  /** Heading for the group, for example "Run 1: your message". Omit for a single flat feed. */
  title?: string;
  active?: boolean;
  events: RunEvent[];
}

const MILESTONE_ICON: Record<string, IconName> = { quote: "quote", approval: "approval", transaction: "transaction", case: "case" };

/** Actor badge: shows the display name (ALDO bot, ALDO Gateway, ...); the technical id stays in the title and data-actor. */
export function ActorBadge({ actor }: { actor: string }) {
  return (
    <span className={`actor actor-${actor}`} title={ACTOR_TITLE[actor] ?? actor} data-testid="actor-badge" data-actor={actor}>
      {actorLabel(actor)}
    </span>
  );
}

function Metrics({ m }: { m: RunMetrics }) {
  const cost = m.cost ? `${m.cost.currency === "USD" ? "US$" : "CA$"}${m.cost.amount.toFixed(m.cost.amount < 0.1 ? 4 : 3)}` : "n/a";
  return (
    <dl className="metrics" aria-label="Run metrics" data-testid="run-metrics">
      <div>
        <dt>Rounds</dt>
        <dd>{formatInt(m.rounds)}</dd>
      </div>
      <div>
        <dt>Tools</dt>
        <dd>{formatInt(m.tools)}</dd>
      </div>
      <div>
        <dt>Tokens</dt>
        <dd>
          {formatInt(m.inputTokens)} in / {formatInt(m.outputTokens)} out
        </dd>
      </div>
      <div>
        <dt>Seconds</dt>
        <dd>{formatSeconds(m.latencyMs)}</dd>
      </div>
      <div>
        <dt>Est. cost</dt>
        <dd>
          {cost}
          {m.cost?.assumed ? <span className="muted"> (assumed price)</span> : null}
        </dd>
      </div>
      {m.status !== "completed" && (
        <div>
          <dt>Status</dt>
          <dd>
            {m.status}
            {m.errorCode ? ` (${m.errorCode})` : ""}
          </dd>
        </div>
      )}
    </dl>
  );
}

function Row({ row, showActors }: { row: TraceRow; showActors: boolean }) {
  const actor = showActors ? <ActorBadge actor={row.actor} /> : null;

  switch (row.kind) {
    case "run_started":
      return (
        <li className="trace-item trace-muted">
          <Icon name="play" size={16} /> {actor} <span>Run started</span>
        </li>
      );

    case "tool": {
      const failed = row.ok === false;
      return (
        <li className={`trace-item trace-tool${failed ? " is-error" : ""}`} data-testid="trace-tool" data-tool={row.tool}>
          <div className="trace-line">
            <Icon name="tool" size={16} />
            {actor}
            <span className="mono strong tool-name">{row.tool}</span>
            {row.pending && (
              <span className="muted">
                <span className="spinner" aria-hidden="true" /> running
              </span>
            )}
            {!row.pending && !failed && (
              <span className="ok-mark">
                <Icon name="check" size={14} /> ok
              </span>
            )}
            {failed && row.code && <ErrorCodeNote code={row.code} />}
            {row.latencyMs !== undefined && <span className="latency muted">{formatLatency(row.latencyMs)}</span>}
          </div>
          {row.args.length > 0 && (
            <p className="trace-args mono muted">{row.args.map((a) => `${a.label.toLowerCase().replace(/ /g, "_")}=${a.value}`).join(", ")}</p>
          )}
          {failed && row.code && (row.message ?? codeHint(row.code)) && <p className="trace-msg">{row.message ?? codeHint(row.code)}</p>}
          {row.evidence.length > 0 && (
            <ul className="chips" aria-label="Evidence">
              {row.evidence.slice(0, 8).map((c) => (
                <li key={c.label} className="chip">
                  <span className="chip-label">{c.label}</span> {c.value}
                </li>
              ))}
            </ul>
          )}
          {row.provenance.length > 0 && (
            <ul className="chips" aria-label="Data provenance">
              {row.provenance.map((p) => (
                <li key={p}>
                  <ProvenanceBadge kind={p} />
                </li>
              ))}
            </ul>
          )}
        </li>
      );
    }

    case "decision":
      return (
        <li className="trace-item trace-decision" data-testid="trace-decision">
          <Icon name="decision" size={16} />
          <div>
            {actor} <span className="strong">Decision</span>
            <p>{row.text}</p>
          </div>
        </li>
      );

    case "milestone":
      return (
        <li className="trace-item trace-milestone" data-testid={`trace-${row.icon}`}>
          <Icon name={MILESTONE_ICON[row.icon]} size={16} />
          <span className="trace-line-inline">
            {actor}
            <span className="strong">{row.label}</span>
          </span>
        </li>
      );

    case "a2a":
      return (
        <li className="trace-item trace-a2a" data-testid="trace-a2a" data-event-type={row.eventType}>
          <Icon name={row.icon} size={16} />
          <div>
            <div className="trace-line">
              {actor}
              <span className="strong">{row.headline}</span>
            </div>
            {row.detail && <p className="trace-detail">{row.detail}</p>}
            {row.provenance.length > 0 && (
              <ul className="chips" aria-label="Data provenance">
                {row.provenance.map((p) => (
                  <li key={p}>
                    <ProvenanceBadge kind={p} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </li>
      );

    case "assistant":
      return (
        <li className="trace-item trace-muted">
          <Icon name="message" size={16} /> {actor} <span>{row.actor === "personal_agent" ? "Replied to the owner" : "Replied to the customer"}</span>
        </li>
      );

    case "warning":
      return (
        <li className="trace-item trace-tool is-error">
          <div className="trace-line">
            <ErrorCodeNote code={row.code} />
            <span>{row.message}</span>
          </div>
        </li>
      );

    case "run_completed":
      return (
        <li className="trace-item trace-complete">
          <Icon name="flag" size={16} /> {actor}
          <span className="strong">Run {row.metrics.status === "completed" ? "completed" : row.metrics.status}</span>
          <Metrics m={row.metrics} />
        </li>
      );
  }
}

interface Props {
  groups: TraceGroup[];
  showActors?: boolean;
  emptyText: string;
  ariaLabel: string;
  testId: string;
  /** Shown when the only group is empty and active. */
  waitingText?: string;
}

/** Ordered event timeline, shared by the A2A feed and both agents' tool traces. */
export function TraceTimeline({ groups, showActors = false, emptyText, ariaLabel, testId, waitingText = "Waiting for the agent to start" }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const built = useMemo(() => groups.map((g) => ({ group: g, rows: buildTraceRows(g.events) })), [groups]);
  const total = built.reduce((n, g) => n + g.rows.length, 0);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [total]);

  function onScroll() {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  return (
    <div ref={scrollRef} onScroll={onScroll} className="trace-scroll" tabIndex={0} role="region" aria-label={ariaLabel} data-testid={testId}>
      {built.length === 0 && <p className="muted empty">{emptyText}</p>}
      {built.map(({ group, rows }) => (
        <div key={group.key} className="trace-run" data-testid="trace-run">
          {group.title && <h4>{group.title}</h4>}
          <ol className="timeline">
            {rows.length === 0 && group.active && (
              <li className="trace-item trace-muted">
                <span className="spinner" aria-hidden="true" /> <span>{waitingText}</span>
              </li>
            )}
            {rows.map((row) => (
              <Row key={`${group.key}-${row.kind}-${row.seq}`} row={row} showActors={showActors} />
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}
