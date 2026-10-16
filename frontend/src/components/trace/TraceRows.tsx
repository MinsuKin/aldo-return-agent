import { useId, useState } from "react";
import { formatLatency, formatSince } from "../../lib/format";
import type { ToolStep } from "../../lib/runs";
import type { EventItem, RunItem } from "../../lib/timeline";
import { Badge, ProvenanceBadge } from "../Badge";
import { Icon } from "../Icon";
import { ActorChip } from "./ActorChip";

/** Time since the first event, or nothing when the item has no timestamp yet. */
function Since({ tsMs, t0 }: { tsMs: number; t0: number | null }) {
  if (t0 === null || !Number.isFinite(tsMs)) return null;
  return (
    <time className="ev-latency" data-testid="trace-time">
      {formatSince(tsMs - t0)}
    </time>
  );
}

export function EventRow({ item, t0 }: { item: EventItem; t0: number | null }) {
  return (
    <li className={`event actor-${item.actor} tone-${item.tone}`} data-testid="trace-event" data-event-type={item.type} data-actor={item.actor}>
      <div className="ev-top">
        <ActorChip actor={item.actor} />
        <Since tsMs={item.tsMs} t0={t0} />
      </div>
      <p className="ev-title">{item.title}</p>
      {item.detail && <p className="ev-detail">{item.detail}</p>}
      {(item.chips.length > 0 || item.provenance.length > 0 || item.flags.length > 0) && (
        <ul className="chips" aria-label="Details">
          {item.flags.map((f) => (
            <li key={f.label}>
              <Badge tone={f.tone === "green" ? "success" : f.tone === "amber" ? "amber" : "neutral"}>{f.label}</Badge>
            </li>
          ))}
          {item.chips.map((c) => (
            <li key={c.label} className="chip">
              <span className="chip-label">{c.label}</span> {c.value}
            </li>
          ))}
          {item.provenance.map((p) => (
            <li key={p}>
              <ProvenanceBadge kind={p} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Step({ step }: { step: ToolStep }) {
  const failed = step.ok === false;
  return (
    <li className={`step${failed ? " step-error" : ""}`} data-testid="trace-step" data-tool={step.tool}>
      <p className="step-line">
        {step.pending ? (
          <span className="spinner" aria-hidden="true" />
        ) : failed ? (
          <Icon name="alert" size={14} />
        ) : (
          <Icon name="check" size={14} />
        )}
        <span className="step-label mono">{step.label}</span>
        <span className="mono step-tool">{step.tool}</span>
        {step.latencyMs !== undefined && <span className="mono step-latency">{formatLatency(step.latencyMs)}</span>}
        {failed && step.code && <span className="code-amber">{step.code}</span>}
      </p>
      {step.hidden ? (
        <p className="step-private">
          <Icon name="lock" size={12} /> Private to ALDO bot: details are not shown here.
        </p>
      ) : (
        <>
          {step.args.length > 0 && (
            <p className="step-args mono">{step.args.map((a) => `${a.label.toLowerCase().replace(/ /g, "_")}=${a.value}`).join(", ")}</p>
          )}
          {step.message && <p className="ev-detail">{step.message}</p>}
          {(step.evidence.length > 0 || step.provenance.length > 0) && (
            <ul className="chips" aria-label="Evidence">
              {step.evidence.slice(0, 8).map((c) => (
                <li key={c.label} className="chip">
                  <span className="chip-label">{c.label}</span> {c.value}
                </li>
              ))}
              {step.provenance.map((p) => (
                <li key={p}>
                  <ProvenanceBadge kind={p} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

/** One run of ALDO bot (purchase or return workflow): its tool calls, collapsed by default. */
export function RunRow({ item, t0, onToggle }: { item: RunItem; t0: number | null; onToggle: () => void }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const count = item.steps.length;
  const summary = item.active
    ? `Working${item.currentTool ? `: ${item.currentTool}` : ""}`
    : `${count} tool ${count === 1 ? "call" : "calls"}${item.latencyMs !== undefined ? ` · ${formatLatency(item.latencyMs)}` : ""}`;

  return (
    <li className={`event trace-run actor-${item.actor}`} data-testid="trace-run" data-run-id={item.runId} data-actor={item.actor} data-state={item.active ? "active" : "done"}>
      <div className="ev-top">
        <ActorChip actor={item.actor} />
        <Since tsMs={item.tsMs} t0={t0} />
      </div>
      <button
        type="button"
        className="run-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => {
          onToggle();
          setOpen((v) => !v);
        }}
        data-testid="run-toggle"
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size={16} />
        {item.active && <span className="spinner" aria-hidden="true" />}
        <span>{summary}</span>
      </button>
      <ol id={panelId} className="steps" hidden={!open} aria-label="Tool calls">
        {item.steps.length === 0 && <li className="step-private">Starting...</li>}
        {item.steps.map((s) => (
          <Step key={s.key} step={s} />
        ))}
      </ol>
    </li>
  );
}
