import { useLayoutEffect, useState, type RefObject } from "react";

/** The phone's design size in CSS px (iPhone 17 Pro Max style, addendum 11.1 D1). */
export const PHONE_W = 440;
export const PHONE_H = 956;
/** The trace keeps at least this much width beside the phone. */
const MIN_TRACE_W = 560;
const MIN_SCALE = 0.4;
const MAX_SCALE = 1.3;

/**
 * Scale that fits the phone into the stage: by height when the phone sits beside the trace (no page
 * scroll), by width only when the two are stacked. In the wide layout the height also leaves
 * `--phone-pad-y` free above and below the phone. The stage's own size never depends on the scale, so
 * observing it cannot feed back.
 */
export function useFitScale(stageRef: RefObject<HTMLElement | null>, wide: boolean): number {
  const [scale, setScale] = useState(wide ? 0.85 : 1);

  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const compute = () => {
      const cs = getComputedStyle(stage);
      const px = (v: string) => Number.parseFloat(v) || 0;
      const innerW = stage.clientWidth - px(cs.paddingLeft) - px(cs.paddingRight);
      const innerH = stage.clientHeight - px(cs.paddingTop) - px(cs.paddingBottom);
      const gap = px(cs.columnGap) || 16;
      // Space kept free above and below the phone inside the striped panel (--phone-pad-y in shell.css).
      const padY = wide ? 2 * px(cs.getPropertyValue("--phone-pad-y")) : 0;
      const byWidth = wide ? (innerW - gap - MIN_TRACE_W) / PHONE_W : innerW / PHONE_W;
      const byHeight = wide ? (innerH - padY) / PHONE_H : Number.POSITIVE_INFINITY;
      const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, byWidth, byHeight));
      setScale((prev) => (Math.abs(prev - next) < 0.002 ? prev : next));
    };
    compute();
    const observer = new ResizeObserver(compute);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [stageRef, wide]);

  return scale;
}
