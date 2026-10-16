import type { RefObject } from "react";
import { colorWord } from "../../lib/format";
import { cleanLabel, outcomeLabel, type MemoryView } from "../../lib/normalize";
import type { WritebackState } from "../../hooks/useWriteback";
import { Badge } from "../Badge";
import { Icon } from "../Icon";
import { PhoneSheet } from "./PhoneSheet";
import { Swatch } from "./cards/shared";

interface Props {
  memory: MemoryView | undefined;
  loading: boolean;
  error: string | null;
  /** ALDO sends its size update after an exchange; it is delivered when this memory is read. */
  writeback: WritebackState;
  onCheckAgain: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
}

/** "What your personal agent remembers": the owner's size memory, plus what ALDO taught it after exchanges. */
export function MemorySheet({ memory, loading, error, writeback, onCheckAgain, returnFocusRef, onClose }: Props) {
  const fits = memory?.updates.filter((u) => u.fitFeedback === "size_ok") ?? [];
  const swaps = memory?.updates.filter((u) => u.fitFeedback !== "size_ok") ?? [];
  const learned = (memory?.updates.length ?? 0) > 0;

  return (
    <PhoneSheet
      kind="memory"
      title="What your personal agent remembers"
      description="Private to your personal agent: ALDO never sees these notes."
      returnFocusRef={returnFocusRef}
      onClose={onClose}
    >
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

          <section aria-labelledby="mem-elsewhere">
            <h3 id="mem-elsewhere">From other retailers</h3>
            {memory.entries.length === 0 ? (
              <p className="muted">Nothing saved yet.</p>
            ) : (
              <ul className="memory-list" data-testid="memory-entries">
                {memory.entries.map((m) => (
                  <li key={m.id}>
                    <span className="strong">{cleanLabel(m.retailer)}</span>: {m.item}
                    {m.color ? (
                      <>
                        {", "}
                        <Swatch color={m.color} /> {colorWord(m.color)}
                      </>
                    ) : null}
                    , size {m.size}, <span className={`outcome outcome-${m.outcome}`}>{outcomeLabel(m.outcome)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="mem-aldo">
            <h3 id="mem-aldo">Learned from ALDO</h3>
            {writeback === "waiting" && (
              <p role="status" className="muted" data-testid="memory-pending">
                <span className="spinner" aria-hidden="true" /> Waiting for ALDO&apos;s size update...
              </p>
            )}
            {writeback === "late" && (
              <div className="alert alert-info" role="status" data-testid="memory-late">
                <p>ALDO&apos;s size update has not arrived yet. It is delivered when this memory is read.</p>
                <button type="button" className="btn btn-small" onClick={onCheckAgain} data-testid="memory-check-again">
                  Check again
                </button>
              </div>
            )}
            {!learned && writeback === "idle" && (
              <p className="muted" data-testid="memory-no-updates">
                Nothing yet. ALDO sends a size update after an exchange.
              </p>
            )}

            {fits.length > 0 && (
              <ul className="memory-list" aria-label="Sizes that fit" data-testid="memory-fits">
                {fits.map((u) => (
                  <li key={u.id} data-testid="memory-fit">
                    <Icon name="check" size={14} /> <strong>Size {u.toSize ?? u.fromSize ?? "?"} fits</strong>{" "}
                    <span className="muted">
                      ({u.product}
                      {u.color ? `, ${colorWord(u.color)}` : ""}; kept when exchanging colour)
                    </span>{" "}
                    <Badge tone={u.provisional ? "amber" : "success"}>{u.provisional ? "provisional" : "confirmed"}</Badge>
                  </li>
                ))}
              </ul>
            )}

            {swaps.length > 0 && (
              <ul className="memory-list" aria-label="Updates from ALDO" data-testid="memory-updates">
                {swaps.map((u) => (
                  <li key={u.id} data-testid="memory-update">
                    <span className="strong">
                      {u.product}
                      {u.color ? ` (${colorWord(u.color)})` : ""}
                    </span>
                    : size {u.fromSize ?? "?"} to <strong>{u.toSize ?? "?"}</strong>{" "}
                    <Badge tone={u.provisional ? "amber" : "success"}>{u.provisional ? "provisional" : "confirmed"}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      <div className="sheet-actions">
        <button type="button" className="btn btn-primary" onClick={onClose} data-testid="memory-done">
          Done
        </button>
      </div>
    </PhoneSheet>
  );
}
