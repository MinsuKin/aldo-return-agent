import { ApiError } from "../api/client";

export interface ErrorInfo {
  code: string;
  message: string;
  /** Suggested follow-up the UI can offer. */
  action?: "requote" | "retry";
}

type Table = Record<string, { message: string; action?: ErrorInfo["action"] }>;

const MESSAGES: Table = {
  NOT_AUTHENTICATED: { message: "Your demo session has ended. Pick a customer again to continue." },
  NOT_AUTHORIZED: { message: "That item belongs to another customer, so it cannot be opened from this sign-in." },
  ORDER_NOT_FOUND: { message: "That order could not be found." },
  QUOTE_EXPIRED: {
    message: "These options have expired. Ask ALDO bot for fresh options, then confirm again.",
    action: "requote",
  },
  IDEMPOTENCY_CONFLICT: {
    message:
      "A different option was already confirmed on this quote. Only one option can be confirmed per quote, so nothing changed.",
  },
  RUN_IN_PROGRESS: {
    message: "ALDO bot is still working on the previous message. Try again in a moment.",
    action: "retry",
  },
  STOCK_CHANGED: {
    message: "Stock changed, so that option is no longer available. ALDO bot will look for another option.",
  },
  APPROVAL_REQUIRED: { message: "That step needs your confirmation first." },
  UPSTREAM_TIMEOUT: { message: "ALDO bot took too long to respond. Please try again.", action: "retry" },
  LIMIT_REACHED: { message: "ALDO bot reached its step limit and handed this to a person." },
  THROTTLED: { message: "The demo is busy. Wait a few seconds and try again.", action: "retry" },
  NETWORK_ERROR: { message: "Cannot reach the demo API. Check the connection and try again.", action: "retry" },
  POLICY_NOT_ELIGIBLE: { message: "These items are no longer eligible, so nothing was changed." },
};

/** Purchase confirmation has its own wording for the codes it can hit. */
const PURCHASE_MESSAGES: Table = {
  APPROVAL_REQUIRED: {
    message: "ALDO bot has not made a proposal for this request yet, so there is nothing to confirm.",
  },
  REQUEST_EXPIRED: {
    message: "This proposal expired, so nothing was ordered. Ask ALDO bot to check again, then confirm the new proposal.",
    action: "requote",
  },
  STOCK_CHANGED: {
    message: "Stock changed, so that option is gone and nothing was ordered. Ask ALDO bot to look again.",
    action: "requote",
  },
  IDEMPOTENCY_CONFLICT: {
    message: "A different option was already confirmed for this request, so nothing changed.",
  },
};

export type ErrorContext = "default" | "purchase";

export function describeError(e: unknown, context: ErrorContext = "default"): ErrorInfo {
  if (e instanceof ApiError) {
    const known = (context === "purchase" ? PURCHASE_MESSAGES[e.code] : undefined) ?? MESSAGES[e.code];
    if (known) return { code: e.code, message: known.message, action: known.action };
    return { code: e.code, message: e.message || `Request failed (${e.status}).` };
  }
  return { code: "UNKNOWN", message: e instanceof Error ? e.message : "Something went wrong." };
}

/** Plain-language meaning of machine error codes shown in the trace. */
export function codeHint(code: string): string | undefined {
  return MESSAGES[code]?.message;
}
