import type { RefObject } from "react";
import { colorWord } from "../../lib/format";
import { cleanLabel, outcomeLabel, type MemoryView } from "../../lib/normalize";
import type { WritebackState } from "../../hooks/useWriteback";
import { PhoneSheet } from "./PhoneSheet";

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

const cap = (s: string): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
/** A cell value: its parts in one line, separated by a middle dot. */
const cell = (...parts: Array<string | false | undefined>): string => parts.filter(Boolean).join(" · ");

/**
 * "What your personal agent remembers": the owner's size memory, plus what ALDO taught it after exchanges.
 * Everything saved is shown in the same label / value table cells as the cards, in one text style.
 */
export function MemorySheet({ memory, loading, error, writeback, onCheckAgain, returnFocusRef, onClose }: Props) {
  const fits = memory?.updates.filter((u) => u.fitFeedback === "size_ok") ?? [];
  const swaps = memory?.updates.filter((u) => u.fitFeedback !== "size_ok") ?? [];
  const learned = (memory?.updates.length ?? 0) > 0;
  const status = (provisional: boolean | undefined): string => (provisional ? "provisional" : "confirmed");

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
            <ul className="cell-table" data-testid="memory-typical">
              <li>
                <span className="cell-label">Typical size</span>
                <span className="cell-value">
                  {memory.typicalSize}
                  {memory.sizeSystem ? ` (${memory.sizeSystem.replace("_", " ")})` : ""}
                </span>
              </li>
            </ul>
          )}

          <section aria-labelledby="mem-elsewhere">
            <h3 id="mem-elsewhere">From other retailers</h3>
            {memory.entries.length === 0 ? (
              <p className="muted">Nothing saved yet.</p>
            ) : (
              <ul className="cell-table" data-testid="memory-entries">
                {memory.entries.map((m) => (
                  <li key={m.id}>
                    <span className="cell-label">{cleanLabel(m.retailer)}</span>
                    <span className="cell-value">
                      {cell(
                        `${cap(m.item)}${m.color ? `, ${colorWord(m.color)}` : ""}, size ${m.size}`,
                        outcomeLabel(m.outcome),
                      )}
                    </span>
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
              <ul className="cell-table" aria-label="Sizes that fit" data-testid="memory-fits">
                {fits.map((u) => (
                  <li key={u.id} data-testid="memory-fit">
                    <span className="cell-label">Size {u.toSize ?? u.fromSize ?? "?"} fits</span>
                    <span className="cell-value">
                      {cell(
                        `${u.product}${u.color ? `, ${colorWord(u.color)}` : ""}`,
                        "kept when exchanging colour",
                        status(u.provisional),
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {swaps.length > 0 && (
              <ul className="cell-table" aria-label="Updates from ALDO" data-testid="memory-updates">
                {swaps.map((u) => (
                  <li key={u.id} data-testid="memory-update">
                    <span className="cell-label">
                      {u.product}
                      {u.color ? ` (${colorWord(u.color)})` : ""}
                    </span>
                    <span className="cell-value">
                      {cell(`Size ${u.fromSize ?? "?"} to ${u.toSize ?? "?"}`, status(u.provisional))}
                    </span>
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
