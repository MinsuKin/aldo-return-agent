import { useEffect, useRef, useState } from "react";
import type { MemoryView } from "../lib/normalize";
import type { WritebackState } from "./PersonalAgentPanel";
import { outcomeLabel } from "../lib/normalize";
import { Badge } from "./Badge";
import { Icon } from "./Icon";

interface Props {
  memory: MemoryView | undefined;
  loading: boolean;
  error: string | null;
  /** ALDO sends its size update after an exchange; it is delivered when this memory is read. */
  writeback: WritebackState;
  onCheckAgain: () => void;
}

const signature = (u: MemoryView["updates"][number]) => `${u.fromSize}>${u.toSize}:${u.provisional}:${u.outcome}`;

/** ALDO bot's private, synthetic size memory, including ALDO write-backs. */
export function MemoryCard({ memory, loading, error, writeback, onCheckAgain }: Props) {
  const seen = useRef<Map<string, string> | null>(null);
  const cardRef = useRef<HTMLElement>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!memory) return;
    const next = new Map(memory.updates.map((u) => [u.id, signature(u)]));
    const prev = seen.current;
    seen.current = next;
    if (!prev) return; // first load: nothing is "new"
    const changed = memory.updates.filter((u) => prev.get(u.id) !== signature(u)).map((u) => u.id);
    if (changed.length === 0) return;
    setFresh(new Set(changed));
    cardRef.current?.scrollIntoView({ block: "start" });
    const t = window.setTimeout(() => setFresh(new Set()), 8000);
    return () => window.clearTimeout(t);
  }, [memory]);

  return (
    <section ref={cardRef} className="card memory" aria-labelledby="memory-title" data-testid="memory-card">
      <div className="card-head">
        <h3 id="memory-title">
          <Icon name="memory" size={18} /> Synthetic demo memory
        </h3>
        <Badge tone="simulated">Simulated data</Badge>
      </div>
      <p className="muted card-sub">
        What ALDO bot remembers about your sizes. Invented for this demo. Private to ALDO bot: ALDO never sees it.
      </p>

      {/* Announced to screen readers when ALDO's size update lands (the row is also highlighted). */}
      <p className="sr-only" role="status" data-testid="memory-announce">
        {fresh.size > 0 ? "ALDO bot memory updated with a size update from ALDO." : ""}
      </p>

      {loading && <p role="status">Loading memory...</p>}
      {error && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}

      {memory && (
        <>
          {memory.typicalSize && (
            <p className="memory-typical" data-testid="memory-typical">
              Typical size <strong>{memory.typicalSize}</strong>
              {memory.sizeSystem ? ` (${memory.sizeSystem.replace("_", " ")})` : ""}
            </p>
          )}
          <ul className="memory-list" aria-label="Past purchases from other retailers" data-testid="memory-entries">
            {memory.entries.map((m) => (
              <li key={m.id}>
                <span className="strong">{m.retailer}</span>: {m.item}, size {m.size}, <span className={`outcome outcome-${m.outcome}`}>{outcomeLabel(m.outcome)}</span>
              </li>
            ))}
          </ul>

          <h4>Learned from ALDO</h4>
          {writeback === "waiting" && (
            <p role="status" className="muted" data-testid="memory-pending">
              <span className="spinner" aria-hidden="true" /> Waiting for ALDO&apos;s size update...
            </p>
          )}
          {writeback === "late" && (
            <div className="alert alert-info" role="status" data-testid="memory-late">
              <p>ALDO&apos;s size update has not arrived yet. It is delivered when ALDO bot&apos;s memory is read.</p>
              <button type="button" className="btn" onClick={onCheckAgain} data-testid="memory-check-again">
                Check again
              </button>
            </div>
          )}
          {memory.updates.length === 0 && writeback !== "idle" ? null : memory.updates.length === 0 ? (
            <p className="muted" data-testid="memory-no-updates">
              Nothing yet. ALDO sends a size update after an exchange.
            </p>
          ) : (
            <ul className="memory-list" aria-label="Updates from ALDO" data-testid="memory-updates">
              {memory.updates.map((u) => (
                <li key={u.id} className={fresh.has(u.id) ? "is-new" : undefined} data-testid="memory-update">
                  <span className="strong">
                    {u.product}
                    {u.color ? ` (${u.color.toLowerCase()})` : ""}
                  </span>
                  : size {u.fromSize ?? "?"} to <strong>{u.toSize ?? "?"}</strong>{" "}
                  <Badge tone={u.provisional ? "amber" : "success"}>{u.provisional ? "provisional" : "confirmed"}</Badge>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
