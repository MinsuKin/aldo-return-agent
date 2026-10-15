import type { RefObject } from "react";
import { formatCents, formatDate } from "../lib/format";
import type { ErrorInfo } from "../lib/errors";
import type { OptionView } from "../lib/normalize";
import { SimulatedBadge } from "./Badge";
import { ConfirmDialog, type DialogFact } from "./ConfirmDialog";

interface Props {
  quoteId: string;
  option: OptionView;
  busy: boolean;
  error: ErrorInfo | null;
  returnFocusRef: RefObject<HTMLElement | null>;
  onConfirm: () => void;
  onCancel: () => void;
  onRequote: () => void;
}

/** Return-approval dialog: repeats the option details; only Confirm calls POST /quotes/{id}/approve. */
export function ApproveDialog({ quoteId, option, busy, error, returnFocusRef, onConfirm, onCancel, onRequote }: Props) {
  const refund = option.isExchange && option.refundCents === 0 ? "None, this is a size exchange" : formatCents(option.refundCents, option.currency);
  const facts: DialogFact[] = [];
  if (option.isExchange) {
    facts.push({ label: "Replacement", value: `${option.productName ?? "Same style"}${option.replacementSize ? `, size ${option.replacementSize}` : ""}` });
  }
  facts.push(
    { label: "Location", value: option.locationName ?? "Not provided" },
    { label: "Refund", value: refund },
    { label: "Fee", value: formatCents(option.feeCents, option.currency) },
    { label: "ETA", value: option.etaDate ? formatDate(option.etaDate) : (option.etaText ?? "At drop-off") },
  );

  return (
    <ConfirmDialog
      kind="approve"
      title="Confirm your choice"
      description="Check the details, then confirm. Nothing happens until you confirm, and this prototype only creates a simulated record."
      heading={
        <>
          {option.typeLabel} <SimulatedBadge />
        </>
      }
      facts={facts}
      bullets={option.conditions}
      footnote={`Quote ${quoteId}, option ${option.optionId}${option.payloadHash ? `, fingerprint ${option.payloadHash.slice(0, 12)}` : ""}`}
      confirmLabel="Confirm and approve"
      busy={busy}
      error={error}
      errorActionLabel="Ask for new options"
      onErrorAction={onRequote}
      returnFocusRef={returnFocusRef}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
