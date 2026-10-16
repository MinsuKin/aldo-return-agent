import { useState } from "react";
import type { Entry } from "../../state/conversation";

export interface ReasonOption {
  id: string;
  /** What the button says. */
  label: string;
  /** The reason as a sentence, without the final full stop. None means "let me type it myself". */
  text?: string;
  /** The line can carry "and they're unworn" (a damaged or defective item is returnable worn or not). */
  withCondition?: boolean;
}

/**
 * Return reasons offered while the ALDO agent waits for one. The owner can tap one or type their own. Found on the live bot
 * (probe of 2026-10-15): for a change of mind, a size or a colour it asks whether the items are unworn before it gives
 * options, and asks again if it is not answered, so the wear question is answered up front by the toggle below.
 */
export const RETURN_REASONS: ReasonOption[] = [
  { id: "changed-mind", label: "Changed my mind", text: "I changed my mind", withCondition: true },
  { id: "defective", label: "Damaged or defective", text: "The product is damaged or defective" },
  { id: "size-too-big", label: "Size too big", text: "The size is too big", withCondition: true },
  { id: "size-too-small", label: "Size too small", text: "The size is too small", withCondition: true },
  { id: "wrong-colour", label: "Wrong colour", text: "The colour is wrong", withCondition: true },
  { id: "other", label: "Something else" },
];

/** The line a tap sends: the reason, plus the wear answer when the toggle says unworn and the reason needs it. */
export function reasonLine(option: ReasonOption, unworn: boolean): string {
  return option.withCondition && unworn ? `${option.text}, and they're unworn.` : `${option.text}.`;
}

/**
 * True when the newest thing in the chat is the agent asking WHY something is returned ("What's the reason?",
 * "Why are you returning them?", "What's wrong with them?"). A list of items ("1. ...") is the "which ones" question,
 * not this one, so David's ten-item reply never shows reasons.
 */
export function isAskingForReason(entries: Entry[]): boolean {
  const last = entries[entries.length - 1];
  if (!last || last.kind !== "bot") return false;
  return /\b(?:reasons?|why|what(?:'s| is| was| went) wrong)\b[^.!?]*\?/i.test(last.text) && !/^\s*\d+\.\s/m.test(last.text);
}

interface Props {
  disabled: boolean;
  /** Send this line as the owner's message. */
  onPick: (text: string) => void;
  /** "Something else": leave the message box to the owner. */
  onOther: () => void;
}

/** A row of tappable reasons above the message box (the next question of the return, answered in one tap). */
export function ReasonChips({ disabled, onPick, onOther }: Props) {
  const [unworn, setUnworn] = useState(true);
  return (
    <div role="group" aria-label="Reasons for the return" className="reasons" data-testid="reason-chips">
      <div className="reasons-head">
        <p className="reasons-hint">Tap a reason, or type your own.</p>
        <label className="reasons-unworn">
          <input type="checkbox" checked={unworn} disabled={disabled} onChange={(e) => setUnworn(e.target.checked)} data-testid="reason-unworn" />
          <span>They're unworn</span>
        </label>
      </div>
      <div className="reasons-row">
        {RETURN_REASONS.map((r) => (
          <button
            key={r.id}
            type="button"
            className={`reason-chip${r.text ? "" : " reason-other"}`}
            disabled={disabled}
            onClick={() => (r.text ? onPick(reasonLine(r, unworn)) : onOther())}
            data-testid={`reason-${r.id}`}
          >
            {r.label}
          </button>
        ))}
      </div>
    </div>
  );
}
