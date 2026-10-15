// Wire types for the API described in design.md section 7 (and contracts.md section c).
// Kept deliberately loose where the contract allows variation; src/lib/normalize.ts maps
// them to the view models the components use. This file is also imported (types only) by
// the mock server, so it must not reference browser-only globals.

export type Provenance = "provided_csv" | "provided_doc" | "synthetic" | "derived";

export interface Persona {
  customer_id: string;
  display_name: string;
  description?: string;
  home_region?: string;
  /** Demo tier (addendum 11.1 D2): "none" | "silver" | "platinum". A demo assumption, not ALDO policy. */
  tier?: string;
  /** Human label of the tier, for example "Silver member". */
  tier_label?: string;
  /** Where the persona lives, for example "Downtown Montreal". */
  location_label?: string;
}

export interface SessionInfo {
  token: string;
  expires_at: string;
  customer: Persona;
}

export interface OrderLine {
  line_id: string;
  sku: string;
  product_name: string;
  size: string;
  color?: string;
  purchased_qty: number;
  returned_or_reserved_qty: number;
  unit_paid_cents: number;
  final_sale: boolean | null;
}

export interface Order {
  order_id: string;
  ordered_at: string;
  delivered_at: string | null;
  currency: string;
  /** "web" for fixture orders, "agent" for orders placed through ALDO bot. */
  channel?: string;
  banner?: string;
  source?: string;
  lines: OrderLine[];
}

/** Who produced a trace event (pivot addendum 5.6 and 11.4). Existing ALDO events default to aldo_agent. */
export type Actor = "personal_agent" | "aldo_gateway" | "aldo_agent" | "aldo_store" | "system";

export interface RunEvent {
  seq: number;
  type: string;
  ts?: string;
  actor?: string;
  payload: Record<string, unknown>;
}

export interface RunEventsResponse {
  run_id?: string;
  status: string;
  events: RunEvent[];
  next_after?: number;
}

/** GET /a2a/events: one long-lived per-owner stream, same paging as run events. */
export interface A2aEventsResponse {
  status?: string;
  events: RunEvent[];
  next_after?: number;
}

export interface ConversationResponse {
  conv_id: string;
}

export interface SendMessageResponse {
  run_id: string;
  conv_id?: string;
  /** Which agent handles this message (addendum 11.5): the purchase workflow or the return workflow. */
  agent?: "personal" | "returns";
  /** Conversation mode when the message was accepted. */
  mode?: "bot" | "returns";
}

/** GET /personal-agent/conversations/{conv} (addendum 11.5). */
export interface ConversationInfo {
  conv_id: string;
  mode: "bot" | "returns";
  returns_order_id?: string | null;
}

export interface ApproveResponse {
  approval_id: string;
  run_id: string;
}

/** Quote as returned by GET /quotes/{id}. Option fields vary slightly, see normalize.ts. */
export interface QuoteWire {
  quote_id: string;
  status?: string;
  expires_at?: string;
  policy_version?: string;
  order_id?: string;
  currency?: string;
  options: Record<string, unknown>[];
  approval?: Record<string, unknown> | null;
  [k: string]: unknown;
}

export interface TransactionWire {
  transaction_id: string;
  state?: string;
  next_step?: string;
  type?: string;
  [k: string]: unknown;
}

export interface CaseWire {
  case_id: string;
  reason_code?: string;
  summary?: string;
  status?: string;
  order_id?: string;
  created_at?: string;
  [k: string]: unknown;
}

export type ResetVariant = "default" | "dc_stockout" | "all_stockout";

/** GET /personal-agent/memory. Shape is normalised tolerantly in lib/normalize.ts. */
export type MemoryWire = Record<string, unknown>;

/** GET /purchases/{size_request_id}: the server's view of ALDO bot's proposal (normalised in lib/normalize.ts). */
export type PurchaseWire = Record<string, unknown>;

/** POST /purchases/{size_request_id}/confirm (contracts.md F.3 SizeDecisionResponse). */
export interface ConfirmPurchaseResponse {
  order_id: string;
  status?: string;
  line?: Record<string, unknown>;
  fulfillment?: Record<string, unknown>;
  next_step?: string;
  replayed?: boolean;
}

export interface ApiErrorBody {
  error: { code: string; message: string };
}
