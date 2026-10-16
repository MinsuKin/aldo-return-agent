import { API_BASE } from "../config";
import { isObj, pick } from "../lib/util";
import { getSession, setSession } from "./session";
import type {
  A2aEventsResponse,
  ApproveResponse,
  CaseWire,
  ConfirmPurchaseResponse,
  ConversationInfo,
  ConversationResponse,
  DataMode,
  DataPlatformStatus,
  MemoryWire,
  Order,
  Persona,
  PurchaseWire,
  QuoteWire,
  ResetVariant,
  RunEventsResponse,
  SendMessageResponse,
  SessionInfo,
  TransactionWire,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

interface RequestOptions {
  body?: unknown;
  auth?: boolean;
  adminSecret?: string;
  signal?: AbortSignal;
}

async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const session = getSession();
  if (opts.auth !== false && session) headers["Authorization"] = `Bearer ${session.token}`;
  if (opts.adminSecret) headers["X-Demo-Admin"] = opts.adminSecret;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "NETWORK_ERROR", "Cannot reach the demo API.");
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const err = isObj(data) && isObj(data.error) ? data.error : undefined;
    const fallbackCode = res.status === 401 ? "NOT_AUTHENTICATED" : res.status === 429 ? "THROTTLED" : `HTTP_${res.status}`;
    const code = typeof err?.code === "string" ? err.code : fallbackCode;
    const message = typeof err?.message === "string" ? err.message : `Request failed (${res.status}).`;
    // An expired or invalid token ends the session; the shell then shows the sign-in screen.
    if (res.status === 401 && opts.auth !== false && code === "NOT_AUTHENTICATED") setSession(null);
    throw new ApiError(res.status, code, message);
  }
  return data as T;
}

function listOf<T>(data: unknown, key: string): T[] {
  if (Array.isArray(data)) return data as T[];
  const inner = pick(data, [key]);
  return Array.isArray(inner) ? (inner as T[]) : [];
}

const enc = encodeURIComponent;

export const api = {
  async personas(): Promise<Persona[]> {
    const data = await request<unknown>("GET", "/demo/personas", { auth: false });
    return listOf<Persona>(data, "personas");
  },

  signIn(customerId: string): Promise<SessionInfo> {
    return request<SessionInfo>("POST", "/demo/sessions", { body: { customer_id: customerId }, auth: false });
  },

  async orders(): Promise<Order[]> {
    const data = await request<unknown>("GET", "/orders");
    return listOf<Order>(data, "orders");
  },

  // ---- Personal agent, A2A feed and demo controls (pivot addendum 5.7) ----

  paCreateConversation(): Promise<ConversationResponse> {
    return request<ConversationResponse>("POST", "/personal-agent/conversations", { body: {} });
  },

  paSendMessage(convId: string, text: string): Promise<SendMessageResponse> {
    return request<SendMessageResponse>("POST", `/personal-agent/conversations/${enc(convId)}/messages`, {
      body: { text },
    });
  },

  /** Conversation mode (addendum 11.5): "bot" = purchase workflow, "returns" = return workflow. */
  paConversation(convId: string): Promise<ConversationInfo> {
    return request<ConversationInfo>("GET", `/personal-agent/conversations/${enc(convId)}`);
  },

  paMemory(): Promise<MemoryWire> {
    return request<MemoryWire>("GET", "/personal-agent/memory");
  },

  a2aEvents(after: number, signal?: AbortSignal): Promise<A2aEventsResponse> {
    return request<A2aEventsResponse>("GET", `/a2a/events?after=${after}`, { signal });
  },

  purchase(sizeRequestId: string): Promise<PurchaseWire> {
    return request<PurchaseWire>("GET", `/purchases/${enc(sizeRequestId)}`);
  },

  confirmPurchase(sizeRequestId: string, optionId: string): Promise<ConfirmPurchaseResponse> {
    return request<ConfirmPurchaseResponse>("POST", `/purchases/${enc(sizeRequestId)}/confirm`, {
      body: { option_id: optionId },
    });
  },

  simulateDelivery(orderId: string): Promise<unknown> {
    return request<unknown>("POST", "/demo/simulate-delivery", { body: { order_id: orderId } });
  },

  simulatePickup(transactionId: string): Promise<unknown> {
    return request<unknown>("POST", "/demo/simulate-pickup", { body: { transaction_id: transactionId } });
  },

  requote(convId: string, quoteId: string): Promise<SendMessageResponse> {
    return request<SendMessageResponse>("POST", `/conversations/${enc(convId)}/requote`, {
      body: { quote_id: quoteId },
    });
  },

  runEvents(runId: string, after: number, signal?: AbortSignal): Promise<RunEventsResponse> {
    return request<RunEventsResponse>("GET", `/runs/${enc(runId)}/events?after=${after}`, { signal });
  },

  quote(quoteId: string): Promise<QuoteWire> {
    return request<QuoteWire>("GET", `/quotes/${enc(quoteId)}`);
  },

  approve(quoteId: string, optionId: string, payloadHash: string): Promise<ApproveResponse> {
    return request<ApproveResponse>("POST", `/quotes/${enc(quoteId)}/approve`, {
      body: { option_id: optionId, payload_hash: payloadHash },
    });
  },

  transaction(transactionId: string): Promise<TransactionWire> {
    return request<TransactionWire>("GET", `/transactions/${enc(transactionId)}`);
  },

  async cases(): Promise<CaseWire[]> {
    const data = await request<unknown>("GET", "/cases");
    return listOf<CaseWire>(data, "cases");
  },

  dataPlatform(adminSecret: string): Promise<DataPlatformStatus> {
    return request<DataPlatformStatus>("GET", "/demo/data-platform", { adminSecret, auth: false });
  },
  setDataPlatform(mode: DataMode, adminSecret: string): Promise<DataPlatformStatus> {
    return request<DataPlatformStatus>("POST", "/demo/data-platform", { body: { mode }, adminSecret, auth: false });
  },
  reset(variant: ResetVariant, adminSecret: string): Promise<unknown> {
    return request<unknown>("POST", "/demo/reset", { body: { variant }, adminSecret, auth: false });
  },
};
