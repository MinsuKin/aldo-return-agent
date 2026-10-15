import { useEffect, useRef, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject } from "react";
import type { ErrorInfo } from "../lib/errors";

export interface DialogFact {
  label: string;
  value: string;
}

export interface ConfirmDialogProps {
  /** "approve" (return options) or "purchase" (ALDO bot proposals): drives ids and test ids. */
  kind: "approve" | "purchase";
  title: string;
  description: string;
  /** Headline of what is being confirmed, repeated from the card. */
  heading: ReactNode;
  facts: DialogFact[];
  bullets?: string[];
  footnote?: string;
  confirmLabel: string;
  busy: boolean;
  error: ErrorInfo | null;
  /** Shown inside the error alert, for example "Ask for new options". */
  errorActionLabel?: string;
  onErrorAction?: () => void;
  /** The button that opened the dialog; focus returns there when the dialog closes. */
  returnFocusRef: RefObject<HTMLElement | null>;
  onConfirm: () => void;
  onCancel: () => void;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Confirmation dialog shared by return approvals and purchase confirmations. Uses a native modal
 * <dialog> (background becomes inert, Escape closes) plus an explicit Tab wrap so focus can never
 * leave the dialog. Only the Confirm button calls the API (the parent owns the mutation).
 */
export function ConfirmDialog(props: ConfirmDialogProps) {
  const { kind, title, description, heading, facts, bullets, footnote, confirmLabel, busy, error, returnFocusRef } = props;
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      // Return focus to the button that opened the dialog, if it can still take focus
      // (after a successful confirmation that button is disabled or gone, so this is a no-op).
      const opener = returnFocusRef.current;
      if (opener && opener.isConnected) opener.focus();
    };
  }, [returnFocusRef]);

  function onKeyDown(e: KeyboardEvent<HTMLDialogElement>) {
    if (e.key !== "Tab") return;
    const items = Array.from(ref.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function onBackdropClick(e: MouseEvent<HTMLDialogElement>) {
    if (e.target === e.currentTarget) props.onCancel();
  }

  const titleId = `${kind}-title`;
  const descId = `${kind}-desc`;

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      onClose={props.onCancel}
      onKeyDown={onKeyDown}
      onClick={onBackdropClick}
      data-testid={`${kind}-dialog`}
    >
      <div className="dialog-body">
        <h2 id={titleId}>{title}</h2>
        <p id={descId}>{description}</p>

        <div className="dialog-summary">
          <p className="dialog-type">{heading}</p>
          <dl className="facts">
            {facts.map((f) => (
              <div key={f.label}>
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
          </dl>
          {bullets && bullets.length > 0 && (
            <ul className="dialog-conditions">
              {bullets.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          )}
          {footnote && <p className="muted mono small">{footnote}</p>}
        </div>

        {error && (
          <div className="alert alert-error" role="alert" data-testid={`${kind}-error`}>
            <p>{error.message}</p>
            {error.action === "requote" && props.errorActionLabel && props.onErrorAction && (
              <button type="button" className="btn" onClick={props.onErrorAction}>
                {props.errorActionLabel}
              </button>
            )}
          </div>
        )}

        <div className="dialog-actions">
          <button type="button" className="btn" onClick={props.onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={props.onConfirm} disabled={busy} data-testid={`confirm-${kind}`}>
            {busy ? "Confirming..." : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
