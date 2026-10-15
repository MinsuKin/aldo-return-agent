import type { ReactNode, RefObject } from "react";
import type { ErrorInfo } from "../../lib/errors";
import { PhoneSheet } from "./PhoneSheet";

export interface SheetFact {
  label: string;
  value: ReactNode;
}

export interface SheetItem {
  name: string;
  detail?: string;
  price?: string;
}

interface Props {
  /** "purchase" (ALDO bot proposals) or "approve" (exchange and return options): drives ids and test ids. */
  kind: "purchase" | "approve";
  title: string;
  description: string;
  /** Headline of what is being confirmed, repeated from the card. */
  heading: ReactNode;
  facts: SheetFact[];
  /** Items of a bundle return. */
  items?: SheetItem[];
  bullets?: string[];
  footnote?: string;
  confirmLabel: string;
  busy: boolean;
  error: ErrorInfo | null;
  /** Shown inside the error alert, for example "Ask for new options". */
  errorActionLabel?: string;
  onErrorAction?: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirmation dialog for purchases and for exchange or return options (the "existing dialog pattern"):
 * it repeats the server-built details, and only its Confirm button calls the API (the parent owns the
 * request). Cancel, Escape or the close button end it without any effect.
 */
export function ConfirmSheet(props: Props) {
  const { kind, heading, facts, items, bullets, footnote, busy, error } = props;
  return (
    <PhoneSheet kind={kind} title={props.title} description={props.description} returnFocusRef={props.returnFocusRef} onClose={props.onCancel}>
      <div className="sheet-summary">
        <p className="sheet-heading">{heading}</p>
        <dl className="facts">
          {facts.map((f) => (
            <div key={f.label}>
              <dt>{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
        {items && items.length > 0 && (
          <ol className="bundle-list" aria-label="Items in this return">
            {items.map((it, i) => (
              <li key={`${it.name}-${i}`}>
                <span className="bundle-name">
                  {it.name}
                  {it.detail ? <span className="muted">{it.detail}</span> : null}
                </span>
                {it.price && <span className="bundle-price">{it.price}</span>}
              </li>
            ))}
          </ol>
        )}
        {bullets && bullets.length > 0 && (
          <ul className="conditions">
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
            <button type="button" className="btn btn-small" onClick={props.onErrorAction}>
              {props.errorActionLabel}
            </button>
          )}
        </div>
      )}

      <div className="sheet-actions">
        <button type="button" className="btn" onClick={props.onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={props.onConfirm} disabled={busy} data-testid={`confirm-${kind}`}>
          {busy ? "Confirming..." : props.confirmLabel}
        </button>
      </div>
    </PhoneSheet>
  );
}
