import { useCallback, useRef, type RefObject } from "react";

/**
 * Follow-the-end bookkeeping for a transcript or a live feed: keep following while the reader is at the
 * end, stop when they scroll away. Scroll events are delivered a frame late, so one caused by our own
 * scrollTop assignment can arrive after more content has grown below; it is ignored (otherwise the feed
 * would stop following by itself).
 */
export function useStickToEnd(
  ref: RefObject<HTMLElement | null>,
  nearPx: number,
  alsoStuck?: (el: HTMLElement) => boolean,
): {
  stick: RefObject<boolean>;
  scrollTo: (top?: number) => void;
  /** Call from onScroll. Returns the new "following" state, or null when the event was our own scroll. */
  onScroll: () => boolean | null;
} {
  const stick = useRef(true);
  const own = useRef<{ top: number; at: number } | null>(null);

  const scrollTo = useCallback(
    (top?: number) => {
      const el = ref.current;
      if (!el) return;
      el.scrollTop = top ?? el.scrollHeight;
      own.current = { top: el.scrollTop, at: performance.now() };
    },
    [ref],
  );

  const onScroll = useCallback((): boolean | null => {
    const el = ref.current;
    if (!el) return null;
    const mine = own.current;
    if (mine && performance.now() - mine.at < 250 && Math.abs(el.scrollTop - mine.top) < 1.5) {
      own.current = null;
      return null;
    }
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < nearPx || (alsoStuck?.(el) ?? false);
    return stick.current;
  }, [ref, nearPx, alsoStuck]);

  return { stick, scrollTo, onScroll };
}
