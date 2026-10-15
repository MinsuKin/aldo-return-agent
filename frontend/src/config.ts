// Build-time configuration (Vite env). No secrets live here.
const env = import.meta.env as Record<string, string | undefined>;

/** API base URL. Default is same-origin `/api` (CloudFront routes it to the HTTP API). */
export const API_BASE: string = (env.VITE_API_BASE ?? "/api").replace(/\/+$/, "");

/** Run-event polling interval while a run is active (requirement 11.2: at most 1 s). */
export const POLL_INTERVAL_MS = 800;

/** Customer message cap enforced by the API (requirement design 7). */
export const MAX_MESSAGE_CHARS = 1000;

/**
 * DEMO_NOW calendar date (America/Toronto) of the fixtures. Only used to word dates as "Today" or
 * "Tomorrow" in the phone; the server stays authoritative for every date it returns.
 */
export const DEMO_TODAY: string = env.VITE_DEMO_TODAY ?? "2026-10-16";

/** The "out for delivery" moment after a purchase is confirmed, before delivery is simulated (addendum 11.1 D10). */
export const DELIVERY_DELAY_MS: number = Number(env.VITE_DELIVERY_DELAY_MS ?? "5000");

/**
 * Assumed Bedrock list prices, USD per million tokens. Only used for the run-metrics
 * footer when a run does not report its own cost. Labelled "assumed price" in the UI.
 */
export const PRICE_IN_PER_MTOK_USD = Number(env.VITE_PRICE_IN_PER_MTOK_USD ?? "3");
export const PRICE_OUT_PER_MTOK_USD = Number(env.VITE_PRICE_OUT_PER_MTOK_USD ?? "15");
