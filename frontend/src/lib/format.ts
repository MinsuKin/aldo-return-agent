import { DEMO_TODAY } from "../config";

/** Money is integer cents. Display only, never arithmetic on floats. */
export function formatCents(cents: number | null | undefined, currency = "CAD"): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "n/a";
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(cents));
  const whole = Math.floor(abs / 100).toLocaleString("en-CA");
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}${currency} ${whole}.${frac}`;
}

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
const DATE_OPTS: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric", year: "numeric" };

/** Calendar dates (YYYY-MM-DD, America/Toronto) render without timezone shifts. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "Not on file";
  const m = DATE_ONLY.exec(value);
  if (m) {
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12));
    return new Intl.DateTimeFormat("en-CA", { ...DATE_OPTS, timeZone: "UTC" }).format(d);
  }
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("en-CA", { ...DATE_OPTS, timeZone: "America/Toronto" }).format(d);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("en-CA", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/Toronto",
  }).format(d);
}

const SHORT_DATE_OPTS: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" };

/** Short calendar date for YYYY-MM-DD values, for example "Fri, Oct 16". */
export function formatShortDate(value: string): string {
  const m = DATE_ONLY.exec(value);
  if (!m) return value;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12));
  return new Intl.DateTimeFormat("en-CA", { ...SHORT_DATE_OPTS, timeZone: "UTC" }).format(d);
}

/** "Today, Fri, Oct 16" / "Tomorrow, Sat, Oct 17" / "Tue, Oct 20" relative to the fixtures' DEMO_NOW date. */
export function friendlyDate(value: string | null | undefined, today: string = DEMO_TODAY): string {
  if (!value) return "Not on file";
  if (!DATE_ONLY.test(value)) return formatDate(value);
  const short = formatShortDate(value);
  const diff = Math.round((Date.parse(`${value}T12:00:00Z`) - Date.parse(`${today}T12:00:00Z`)) / 86_400_000);
  if (diff === 0) return `Today, ${short}`;
  if (diff === 1) return `Tomorrow, ${short}`;
  return short;
}

/** "+3.2 s" below one minute, "+1:12" above. */
export function formatSince(ms: number): string {
  const safe = Math.max(0, ms);
  if (safe < 60_000) return `+${(safe / 1000).toFixed(1)} s`;
  const total = Math.round(safe / 1000);
  return `+${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

const COLOR_HEX: Record<string, string> = {
  black: "#1c1e21",
  brown: "#6b4423",
  tan: "#c9a27b",
  white: "#f4f4f2",
  navy: "#1f2d4d",
  grey: "#8b9098",
  gray: "#8b9098",
  red: "#a3262a",
  blue: "#2c5aa0",
  green: "#2f6b45",
  beige: "#d9c7a8",
  burgundy: "#6d1f2c",
};

/** Swatch colour for a product colour name; unknown names get a neutral grey. */
export function colorHex(name: string | undefined): string {
  return (name && COLOR_HEX[name.trim().toLowerCase()]) || "#9ca3af";
}

/** Product colour names arrive upper-case ("BROWN"); show them as words. */
export function colorWord(name: string | undefined): string {
  return name ? name.trim().toLowerCase() : "";
}

export function formatLatency(ms: number | undefined): string {
  if (ms === undefined) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatSeconds(ms: number | undefined): string {
  if (ms === undefined) return "n/a";
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatCountdown(msRemaining: number): string {
  const total = Math.max(0, Math.floor(msRemaining / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatInt(n: number | undefined): string {
  return n === undefined ? "n/a" : n.toLocaleString("en-CA");
}

/** Compact value for evidence chips. */
export function formatScalar(v: unknown): string {
  if (v === null || v === undefined) return "none";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(3)));
  if (typeof v === "string") return v.length > 60 ? `${v.slice(0, 57)}...` : v;
  if (Array.isArray(v)) return v.map((x) => formatScalar(x)).join(", ");
  const json = JSON.stringify(v);
  return json.length > 60 ? `${json.slice(0, 57)}...` : json;
}
