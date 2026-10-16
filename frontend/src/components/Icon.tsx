import type { ReactNode } from "react";

export type IconName =
  | "tool"
  | "check"
  | "alert"
  | "decision"
  | "quote"
  | "approval"
  | "transaction"
  | "case"
  | "message"
  | "play"
  | "flag"
  | "clock"
  | "signal"
  | "question"
  | "memory"
  | "handoff"
  | "reviews"
  | "store"
  | "benefit"
  | "delivered"
  | "policy"
  | "preferences"
  | "chevron-right"
  | "chevron-down"
  | "send"
  | "back"
  | "user"
  | "truck"
  | "bell"
  | "close"
  | "lock"
  | "hierarchy";

// Simple 24x24 stroke icons. Decorative: meaning is always carried by adjacent text.
const PATHS: Record<IconName, ReactNode> = {
  tool: <path d="M4 17l6-6-6-6M12 19h8" />,
  check: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12.5l3 3 5-6" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3.5l9.5 16.5h-19z" />
      <path d="M12 10v4.5M12 17.5h.01" />
    </>
  ),
  decision: (
    <>
      <path d="M9 18h6M10 21h4" />
      <path d="M12 3a6 6 0 00-3.5 10.9c.7.6 1 1.3 1 2.1h5c0-.8.3-1.5 1-2.1A6 6 0 0012 3z" />
    </>
  ),
  quote: (
    <>
      <path d="M6 3h8l4 4v14H6z" />
      <path d="M14 3v4h4M9 12h6M9 16h6" />
    </>
  ),
  approval: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20v-1.5A4.5 4.5 0 017 14h4a4.5 4.5 0 014.5 4.5V20M16 11.5l2 2 4-4" />
    </>
  ),
  transaction: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M4 7.5l8 4.5 8-4.5M12 12v9" />
    </>
  ),
  case: (
    <>
      <path d="M5 21V4M5 4h11l-2 4 2 4H5" />
    </>
  ),
  message: <path d="M4 5h16v11H9l-5 4z" />,
  play: <path d="M7 4l12 8-12 8z" />,
  flag: (
    <>
      <path d="M5 21V4M5 4h13v9H5" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  signal: <path d="M5 20v-6M12 20V5M19 20V10" />,
  question: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9.5a2.5 2.5 0 015 .5c0 1.7-2.5 2-2.5 3.5M12 17h.01" />
    </>
  ),
  memory: (
    <>
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
    </>
  ),
  handoff: <path d="M7 7h12l-3-3M17 17H5l3 3" />,
  reviews: (
    <>
      <path d="M4 5h16v11H9l-5 4z" />
      <path d="M8 9h8M8 12.5h5" />
    </>
  ),
  store: (
    <>
      <path d="M3 9l2-5h14l2 5H3z" />
      <path d="M5 9v11h14V9M10 20v-6h4v6" />
    </>
  ),
  benefit: <path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.5 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z" />,
  delivered: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M8.5 12l2.5 2.5 4.5-5" />
    </>
  ),
  policy: (
    <>
      <path d="M12 3l8 3v6c0 5-3.5 8.5-8 9.5C7.5 20.5 4 17 4 12V6z" />
      <path d="M9 12l2 2 4-4" />
    </>
  ),
  preferences: <path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M8 15v4" />,
  "chevron-right": <path d="M9 6l6 6-6 6" />,
  "chevron-down": <path d="M6 9l6 6 6-6" />,
  send: <path d="M12 19V6M6 12l6-6 6 6" />,
  back: <path d="M15 5l-7 7 7 7" />,
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0116 0" />
    </>
  ),
  truck: (
    <>
      <path d="M2 7h12v9H2zM14 10h4.5L21 13v3h-7" />
      <circle cx="6.5" cy="17.5" r="2" />
      <circle cx="17.5" cy="17.5" r="2" />
    </>
  ),
  bell: <path d="M6 16v-5a6 6 0 1112 0v5l1.5 2h-15zM10 20.5a2 2 0 004 0" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 118 0v3" />
    </>
  ),
  /* frame icons */
  /* trace icons */
  hierarchy: (
    <>
      <rect x="9" y="3" width="6" height="4" rx="1" />
      <rect x="3" y="17" width="6" height="4" rx="1" />
      <rect x="15" y="17" width="6" height="4" rx="1" />
      <path d="M12 7v4M6 17v-3h12v3M12 11v3" />
    </>
  ),
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
