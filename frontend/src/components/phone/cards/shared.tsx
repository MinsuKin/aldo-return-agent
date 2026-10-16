import { useEffect, useMemo, useRef, type ReactNode, type Ref, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import type { Order } from "../../../api/types";
import { useNow } from "../../../hooks/useNow";
import { colorHex, formatCountdown } from "../../../lib/format";
import { Icon } from "../../Icon";

export interface LineFacts {
  productName?: string;
  color?: string;
  size?: string;
}

/** The signed-in customer's orders, with a lookup from order line id to what the line is. */
export function useOrderLookup(): { orders: Order[] | undefined; lookup: (lineId: string) => LineFacts | undefined } {
  const query = useQuery({ queryKey: ["orders"], queryFn: api.orders, retry: 1 });
  const lookup = useMemo(
    () => (lineId: string) => {
      for (const o of query.data ?? []) {
        const l = o.lines.find((x) => x.line_id === lineId);
        if (l) return { productName: l.product_name, color: l.color, size: l.size };
      }
      return undefined;
    },
    [query.data],
  );
  return { orders: query.data, lookup };
}

/** Small colour dot for a product colour name ("BROWN"). Decorative: the colour is always written next to it. */
export function Swatch({ color }: { color: string | undefined }) {
  return <span className="swatch" style={{ background: colorHex(color) }} aria-hidden="true" />;
}

interface FrameProps {
  title: string;
  titleId: string;
  headingRef?: Ref<HTMLHeadingElement>;
  /** Right side of the header, for example a reference id. */
  meta?: ReactNode;
  tone?: "default" | "success" | "warn";
  testId: string;
  status?: string;
  children: ReactNode;
}

/** The frame every inline card shares: a labelled section with a focusable heading. */
export function CardFrame({ title, titleId, headingRef, meta, tone = "default", testId, status, children }: FrameProps) {
  return (
    <section className={`msg-card msg-card-${tone}`} aria-labelledby={titleId} data-testid={testId} data-status={status}>
      <header className="msg-card-head">
        <h3 id={titleId} ref={headingRef} tabIndex={-1}>
          {title}
        </h3>
        {meta}
      </header>
      {children}
    </section>
  );
}

/**
 * Ticking expiry. The ticking text is hidden from assistive technology (it would be announced every
 * second inside the live transcript); a static "at 3:42 PM" sentence is read instead.
 */
export function Countdown({ expiresAt, active, label, testId }: { expiresAt: number | null; active: boolean; label: string; testId?: string }) {
  const now = useNow(active && expiresAt !== null);
  if (!active || expiresAt === null) return null;
  const remaining = expiresAt - now;
  if (remaining <= 0) return null;
  const clock = new Intl.DateTimeFormat("en-CA", { hour: "numeric", minute: "2-digit" }).format(expiresAt);
  const soon = remaining <= 5 * 60 * 1000;
  return (
    <p className={`card-expiry${soon ? " card-expiry-soon" : ""}`} data-testid={testId}>
      <Icon name="clock" size={14} />
      <span aria-hidden="true">
        {label} in {formatCountdown(remaining)}
      </span>
      <span className="sr-only">
        {label} at {clock}
      </span>
    </p>
  );
}

/**
 * When a card first becomes ready for the owner, move keyboard focus to its heading (so the next Tab
 * reaches its button) unless the owner is working somewhere else on the page.
 */
export function useFocusWhenReady(ref: RefObject<HTMLElement | null>, ready: boolean): void {
  const done = useRef(false);
  useEffect(() => {
    if (!ready || done.current) return;
    done.current = true;
    const el = ref.current;
    if (!el) return;
    const active = document.activeElement;
    const screen = el.closest(".phone-screen");
    if (!active || active === document.body || screen?.contains(active)) el.focus({ preventScroll: true });
  }, [ready, ref]);
}
