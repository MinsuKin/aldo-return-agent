import { useContext, useEffect, useRef, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../Icon";
import { PhoneScreenContext } from "./PhoneFrame";

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

interface Props {
  /** Prefix for ids and test ids: "purchase", "approve" or "memory". */
  kind: string;
  title: string;
  description?: string;
  /** The control that opened the sheet; focus returns there when the sheet closes. */
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
}

/**
 * A modal sheet that slides up inside the phone. The rest of the phone is inert while it is open (the
 * parent sets `inert`), Tab wraps inside the sheet, and Escape or a tap on the dimmed area closes it.
 */
export function PhoneSheet({ kind, title, description, returnFocusRef, onClose, children }: Props) {
  const screen = useContext(PhoneScreenContext);
  const ref = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  // Move focus into the sheet on open and back to the opener on close.
  useEffect(() => {
    if (!screen) return;
    const opener = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    const first = dialog?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? dialog)?.focus();
    return () => {
      const target = returnFocusRef.current ?? opener;
      if (target && target.isConnected) {
        // The phone content is inert while the sheet is open and React lifts that only after this cleanup,
        // so lift it here; an inert element cannot take focus.
        const host = target.closest<HTMLElement>("[inert]");
        if (host) host.inert = false;
        // Do not scroll: the transcript is following the owner's next step, and the opener is where they already were.
        target.focus({ preventScroll: true });
      }
    };
  }, [screen, returnFocusRef]);

  // Escape closes the sheet even when focus is not inside it.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
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

  function onScrimClick(e: MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) onClose();
  }

  if (!screen) return null;
  const titleId = `${kind}-title`;
  const descId = `${kind}-desc`;

  return createPortal(
    <div className="sheet-scrim" onClick={onScrimClick} data-testid={`${kind}-scrim`}>
      <div
        ref={ref}
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        data-testid={`${kind}-dialog`}
      >
        <div className="sheet-grabber" aria-hidden="true" />
        <div className="sheet-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close" data-testid={`${kind}-close`}>
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="sheet-body">
          {description && (
            <p id={descId} className="muted">
              {description}
            </p>
          )}
          {children}
        </div>
      </div>
    </div>,
    screen,
  );
}
