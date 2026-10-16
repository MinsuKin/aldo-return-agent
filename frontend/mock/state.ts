import { createHash, randomBytes } from "node:crypto";
import {
  CONTRAST,
  DEMO_TODAY,
  HERO,
  LOCATIONS,
  MEMORY_SEEDS,
  PERSONAS,
  POLICY_VERSION,
  addDays,
  baseOrders,
  buildInventory,
  daysBetween,
  locationById,
  sizeStr,
  skuFor,
  weekdayText,
  type MockLine,
  type MockLocation,
  type MockOrder,
  type Variant,
} from "./data.ts";

export const SCALE = Number(process.env.MOCK_TIME_SCALE ?? "1");
export const QUOTE_TTL_MS = Number(process.env.MOCK_QUOTE_TTL_MS ?? String(15 * 60 * 1000));
export const MODEL_ID = "us.anthropic.claude-sonnet-5-5";

/** How long a queued write-back must wait before a memory read delivers it (mock control: /__mock/writeback-delay). */
export const writebackDelay = { ms: Number(process.env.MOCK_WRITEBACK_DELAY_MS ?? "0") };

export const now = (): number => Date.now();
/** ISO timestamp with milliseconds, so events a fraction of a second apart keep their order and spacing. */
export const iso = (ms: number): string => new Date(ms).toISOString().replace("Z", "+00:00");
export const hex = (n: number): string => randomBytes(Math.ceil(n / 2)).toString("hex").slice(0, n).toUpperCase();
export const newId = (prefix: string, n = 6): string => `${prefix}${hex(n)}`;

// ---- Types ------------------------------------------------------------------------------

export interface ScriptEvent {
  seq: number;
  type: string;
  actor?: string;
  payload: Record<string, unknown>;
  /** Milliseconds after the run started (already time-scaled). */
  at: number;
  apply?: () => void;
  applied: boolean;
}

export type RunKind = "customer" | "approval" | "requote" | "personal";

export interface Run {
  run_id: string;
  customer_id: string;
  conv_id: string;
  kind: RunKind;
  startedAt: number;
  events: ScriptEvent[];
}

/** What the return workflow knows about the current return (addendum 11.5; one chat, two workflows). */
export interface RetMemo {
  orderId?: string;
  reason?: string;
  condition?: "unworn" | "worn";
  color?: string;
  channel?: "store" | "mail";
  lineIds: string[];
  /** Reason and condition of the previous return, reused for "also return the grey sneaker". */
  lastReason?: string;
  lastCondition?: "unworn" | "worn";
  latestQuoteId?: string;
  /** "too_big" / "too_small" for a fit return. */
  direction?: "too_big" | "too_small";
  /** get_order_context already ran for this return. */
  contextRead?: boolean;
  /** The policy checks already run for these facts (so the trace shows each check once). */
  policyKey?: string;
  policyIds?: string[];
  /** The customer already asked to waive a fee or override the policy in this return. */
  overrideAsked?: boolean;
}

export const newRetMemo = (): RetMemo => ({ lineIds: [] });

export interface Conv {
  conv_id: string;
  customer_id: string;
  agent: "aldo" | "personal";
  order_id: string | null;
  active_run_id: string | null;
  /** Remembered between turns so a follow-up can answer a clarifying question (legacy return flows). */
  memo: { orderId?: string; reason?: string; direction?: string; deadline?: string | null; latestQuoteId?: string };
  /** "bot" = purchase workflow, "returns" = return workflow (personal conversations only). */
  mode: "bot" | "returns";
  returns_order_id: string | null;
  ret: RetMemo;
}

export const newConv = (conv_id: string, customer_id: string, agent: Conv["agent"], order_id: string | null = null): Conv => ({
  conv_id,
  customer_id,
  agent,
  order_id,
  active_run_id: null,
  memo: {},
  mode: "bot",
  returns_order_id: null,
  ret: newRetMemo(),
});

export interface BundleLine {
  line_id: string;
  sku: string;
  product_name: string;
  category?: string;
  color: string;
  size: string;
  qty: number;
  refund_cents: number;
  unit_paid_cents: number;
}

export interface Benefit {
  type: "free_return_shipping";
  label: string;
  saved_cents: number;
  used: number;
  per_month: number;
  note: string;
}

export interface QuoteOption {
  option_id: string;
  type: string;
  sku: string;
  replacement_size: string | null;
  /** Colour exchange: the colour the customer gets (same style and size). */
  replacement_color?: string | null;
  original_size?: string | null;
  location_id: string;
  location_name: string;
  qty: number;
  refund_cents: number;
  fee_cents: number;
  currency: string;
  eta_date: string;
  eta_text: string;
  meets_deadline: boolean | null;
  /** Travel time to the store in minutes, when known (addendum 11.1 D6). */
  minutes_away?: number | null;
  /** Street address of the store, when the location record has one. */
  location_address?: string;
  conditions: string[];
  evidence: Record<string, unknown>;
  source_refs: string[];
  provenance: string;
  /** Bundle option: the items of one shipment (addendum 11.1 D9). */
  lines?: BundleLine[];
  benefit?: Benefit | null;
  ranking_note?: string;
  payload_hash?: string;
}

export interface Approval {
  approval_id: string;
  quote_id: string;
  option_id: string;
  payload_hash: string;
  run_id: string;
  approved_at: string;
  consumed_txn_id: string | null;
}

export interface Quote {
  quote_id: string;
  customer_id: string;
  order_id: string;
  line_id: string;
  conv_id: string;
  run_id: string;
  policy_check_id: string;
  candidate_set_id: string;
  policy_version: string;
  currency: string;
  created_at: string;
  expiresAtMs: number;
  stored_status: "open" | "executed" | "superseded";
  options: QuoteOption[];
  approval: Approval | null;
  /** Built by the colour exchange / bundle engine (not the legacy fit-exchange engine). */
  v2?: boolean;
  /** Lines the quote covers (a bundle covers several). */
  line_ids?: string[];
}

export interface StoreNotice {
  store_id: string;
  store_name: string;
  eta_minutes: number | null;
  summary: string;
  notified_at: string;
}

export interface Txn {
  transaction_id: string;
  customer_id: string;
  state: string;
  option_type: string;
  order_id: string;
  line_id: string;
  qty: number;
  sku: string;
  location_id: string;
  location_name: string;
  replacement_size: string | null;
  replacement_color?: string | null;
  refund_cents: number;
  fee_cents: number;
  currency: string;
  eta_date: string;
  eta_text: string;
  next_step: string;
  simulated: boolean;
  created_at: string;
  quote_id: string;
  update_id: string | null;
  /** Bundle return: one transaction, several lines. */
  lines?: BundleLine[];
  benefit?: Benefit | null;
  /** Created in the same transaction as an in-store exchange or return (addendum 11.1 D7). */
  store_notice?: StoreNotice | null;
}

export interface CaseRecord {
  case_id: string;
  customer_id: string;
  reason_code: string;
  summary: string;
  order_id: string | null;
  line_id: string | null;
  status: string;
  created_at: string;
  run_id: string | null;
}

export interface MemoryUpdate {
  update_id: string;
  order_id: string;
  style_id: string;
  product_name: string;
  color: string;
  original_size: string;
  new_size: string;
  reason: string;
  outcome: string;
  provisional: boolean;
  /** "size_ok" when the owner kept the same size while exchanging colour (addendum 11.1 D11). */
  fit_feedback?: string | null;
  source: string;
  ts: string;
}

export interface A2aEvent {
  seq: number;
  type: string;
  ts: string;
  actor: string;
  payload: Record<string, unknown>;
}

/** One option ALDO's gateway offered for a size request (contracts.md F.3 SizeOption plus the SKU). */
export interface OfferedOption {
  option_id: string;
  size: string;
  type: "ship_from_dc" | "ship_from_store" | "store_pickup";
  sku: string;
  location_id: string;
  location_name: string;
  eta_date: string;
  eta_text: string;
  meets_deadline: boolean | null;
  atp: number;
}

/** SIZEREQ item: open -> proposed (by the personal agent) -> decided (only by the owner's confirm click). */
export interface SizeRequest {
  size_request_id: string;
  customer_id: string;
  style_id: string;
  product_name: string;
  color: string;
  price_cents: number;
  currency: string;
  deadline_date: string | null;
  fit_signal: { signal: string; too_small_share: number; n_fit: number; scope: string; provenance: string };
  offered: OfferedOption[];
  status: "open" | "proposed" | "decided";
  proposed_option_id: string | null;
  rationale_public: string | null;
  confidence: string | null;
  order_id: string | null;
  chosen_option_id: string | null;
  next_step: string | null;
  created_at: string;
  expiresAtMs: number;
}

// ---- State ------------------------------------------------------------------------------

export interface MockState {
  variant: Variant;
  orders: MockOrder[];
  inventory: Map<string, number>;
  convs: Map<string, Conv>;
  runs: Map<string, Run>;
  quotes: Map<string, Quote>;
  txns: Map<string, Txn>;
  cases: CaseRecord[];
  a2a: Map<string, A2aEvent[]>;
  memoryUpdates: Map<string, MemoryUpdate[]>;
  /** ALDO -> the personal agent write-backs waiting to be delivered (A2AOUT# items). */
  outbox: Map<string, PendingWriteback[]>;
  sizeRequests: Map<string, SizeRequest>;
  /** Monthly benefit ledger: `${customer_id}|${yyyy-mm}` -> perks used (addendum 11.1 D8). */
  ledger: Map<string, number>;
  auditCount: number;
  /** Admin setting of the AWS data platform door (SSM in the real system): reset does not touch it. */
  dataMode: "static" | "platform" | "shadow";
}

export const state: MockState = {
  variant: (process.env.MOCK_VARIANT as Variant | undefined) ?? "dc_stockout",
  orders: [],
  inventory: new Map(),
  convs: new Map(),
  runs: new Map(),
  quotes: new Map(),
  txns: new Map(),
  cases: [],
  a2a: new Map(),
  memoryUpdates: new Map(),
  outbox: new Map(),
  sizeRequests: new Map(),
  ledger: new Map(),
  auditCount: 0,
  dataMode: "static",
};

export function resetState(variant: Variant): { deleted: number; written: number } {
  const deleted = state.convs.size + state.runs.size + state.quotes.size + state.txns.size + state.cases.length + state.sizeRequests.size;
  state.variant = variant;
  state.orders = baseOrders();
  state.inventory = buildInventory(variant);
  state.convs = new Map();
  state.runs = new Map();
  state.quotes = new Map();
  state.txns = new Map();
  state.cases = [];
  state.a2a = new Map();
  state.memoryUpdates = new Map();
  state.outbox = new Map();
  writebackDelay.ms = Number(process.env.MOCK_WRITEBACK_DELAY_MS ?? "0");
  state.sizeRequests = new Map();
  state.ledger = new Map();
  return { deleted, written: state.orders.length + state.inventory.size };
}

export const persona = (customerId: string) => PERSONAS.find((p) => p.customer_id === customerId);
export const ordersOf = (customerId: string): MockOrder[] =>
  state.orders.filter((o) => o.customer_id === customerId).sort((a, b) => (a.ordered_at === b.ordered_at ? (a.order_id < b.order_id ? 1 : -1) : a.ordered_at < b.ordered_at ? 1 : -1));
export const orderById = (orderId: string): MockOrder | undefined => state.orders.find((o) => o.order_id === orderId);

export const atp = (sku: string, loc: string): number => state.inventory.get(`${sku}|${loc}`) ?? 0;
export function takeStock(sku: string, loc: string, qty: number): void {
  const key = `${sku}|${loc}`;
  state.inventory.set(key, Math.max(0, (state.inventory.get(key) ?? 0) - qty));
}

// ---- A2A stream and memory --------------------------------------------------------------

/** While a scripted event's side effect runs, this is the time the event was scripted for. */
export const clock = { at: 0 };

export function pushA2a(customerId: string, type: string, actor: string, payload: Record<string, unknown>): void {
  const list = state.a2a.get(customerId) ?? [];
  list.push({ seq: list.length + 1, type, ts: iso(clock.at || now()), actor, payload });
  state.a2a.set(customerId, list);
}

export function memoryFor(customerId: string) {
  const seed = MEMORY_SEEDS.find((s) => s.owner_id === customerId);
  return {
    owner_id: customerId,
    display_name: seed?.display_name ?? "Owner",
    size_system: seed?.size_system ?? "US_W",
    typical_size: seed?.typical_size ?? null,
    label: "personal agent memory",
    memory: seed?.memory ?? [],
    updates: state.memoryUpdates.get(customerId) ?? [],
  };
}

export function addMemoryUpdate(customerId: string, u: MemoryUpdate): void {
  const list = state.memoryUpdates.get(customerId) ?? [];
  list.push(u);
  state.memoryUpdates.set(customerId, list);
}

export interface PendingWriteback {
  update: MemoryUpdate;
  queuedAt: number;
}

/** An exchange was created: the write-back waits in the outbox until something reads the personal agent's memory. */
export function enqueueWriteback(customerId: string, update: MemoryUpdate): void {
  const list = state.outbox.get(customerId) ?? [];
  list.push({ update, queuedAt: now() });
  state.outbox.set(customerId, list);
}

/** Deliver due write-backs (the real API does this before it reads memory). Applying one emits memory_updated. */
export function flushOutbox(customerId: string): number {
  const list = state.outbox.get(customerId) ?? [];
  const due = list.filter((w) => now() - w.queuedAt >= writebackDelay.ms);
  state.outbox.set(
    customerId,
    list.filter((w) => !due.includes(w)),
  );
  for (const w of due) {
    const u = w.update;
    addMemoryUpdate(customerId, u);
    pushA2a(customerId, "memory_updated", "personal_agent", {
      update_id: u.update_id,
      style_id: u.style_id,
      product_name: u.product_name,
      color: u.color,
      original_size: u.original_size,
      new_size: u.new_size,
      reason: u.reason,
      outcome: u.outcome,
      provisional: u.provisional,
      ...(u.fit_feedback ? { fit_feedback: u.fit_feedback } : {}),
    });
  }
  return due.length;
}

// ---- Run script builder -----------------------------------------------------------------

export interface Script {
  events: ScriptEvent[];
  emit: (delayMs: number, type: string, payload: Record<string, unknown>, apply?: () => void, actor?: string) => Script;
  total: () => number;
}

export function script(): Script {
  const events: ScriptEvent[] = [];
  let t = 0;
  const s: Script = {
    events,
    emit(delayMs, type, payload, apply, actor) {
      t += Math.round(delayMs * SCALE);
      events.push({ seq: events.length + 1, type, actor, payload, at: t, apply, applied: false });
      return s;
    },
    total: () => t,
  };
  return s;
}

export interface ToolOutcome {
  ok?: boolean;
  code?: string;
  latency?: number;
  evidence?: Record<string, unknown>;
  provenance?: string;
  refs?: string[];
}

/** tool_call then tool_result, as the real tool wrapper emits them. */
export function toolStep(
  s: Script,
  tool: string,
  args: Record<string, unknown>,
  outcome: ToolOutcome,
  opts: { think?: number; round: number; actor?: string; onCall?: () => void; onResult?: () => void },
): void {
  const latency = outcome.latency ?? 160;
  s.emit(opts.think ?? 650, "tool_call", { tool, args, round: opts.round }, opts.onCall, opts.actor);
  s.emit(
    latency,
    "tool_result",
    {
      tool,
      ok: outcome.ok ?? true,
      code: outcome.code ?? null,
      latency_ms: Math.round(latency),
      evidence: outcome.evidence ?? {},
      source_refs: outcome.refs ?? [],
      provenance: outcome.provenance ?? "synthetic",
      round: opts.round,
    },
    opts.onResult,
    opts.actor,
  );
}

export function finishRun(s: Script, opts: { rounds: number; tools: number; error?: string; actor?: string }): void {
  const total = Math.round(s.total() / (SCALE || 1)) + 600;
  s.emit(
    250,
    "run_completed",
    {
      status: opts.error ? "error" : "completed",
      rounds: opts.rounds,
      tool_count: opts.tools,
      input_tokens: 2100 * opts.rounds + 380,
      output_tokens: 95 * opts.rounds + 120,
      total_latency_ms: total,
      model_id_used: MODEL_ID,
      fallback_used: false,
      error_code: opts.error ?? null,
    },
    undefined,
    opts.actor,
  );
}

export const runEndAt = (run: Run): number => run.startedAt + (run.events.at(-1)?.at ?? 0);
export const runDone = (run: Run, t = now()): boolean => t >= runEndAt(run);

export function runStatus(run: Run, t = now()): string {
  if (runDone(run, t)) {
    const last = run.events.at(-1);
    return last?.payload.status === "error" ? "error" : "completed";
  }
  const first = run.events[0];
  return first && t >= run.startedAt + first.at ? "running" : "queued";
}

/** Apply side effects of scripted events as they become visible, in order. */
export function materialize(t = now()): void {
  for (const run of state.runs.values()) {
    for (const ev of run.events) {
      if (ev.applied) continue;
      if (run.startedAt + ev.at > t) break;
      ev.applied = true;
      clock.at = run.startedAt + ev.at;
      try {
        ev.apply?.();
      } finally {
        clock.at = 0;
      }
    }
  }
}

// ---- Policy and options -------------------------------------------------------------------

export interface PolicyOutcome {
  decision: "eligible" | "ineligible" | "needs_information" | "needs_review";
  rule_ids: string[];
  missing_fields: string[];
  allowed_option_types: string[];
  policy_version: string;
}

const EXCHANGES = ["exchange_ship_from_dc", "exchange_ship_from_store", "exchange_store_pickup"];
const RETURNS = ["return_in_store", "return_mail"];

export function evaluatePolicy(order: MockOrder, line: MockLine, qty: number, reason: string, condition: string): PolicyOutcome {
  const rules: string[] = [];
  const missing: string[] = [];
  let decision: PolicyOutcome["decision"] = "eligible";
  const terminal = (d: PolicyOutcome["decision"], rule: string) => {
    rules.push(rule);
    if (decision === "eligible") decision = d;
  };
  const isDefect = reason === "QUALITY_DAMAGE";
  if (line.final_sale === true && !isDefect) terminal("ineligible", "final_sale.no_return");
  else if (line.final_sale === true && isDefect) terminal("needs_review", "final_sale.defect_review");
  if (qty > line.purchased_qty - line.returned_or_reserved_qty) terminal("ineligible", "qty.exceeds_remaining");
  if (!order.delivered_at) terminal("needs_review", "window.missing_delivery_date");
  else if (daysBetween(order.delivered_at, DEMO_TODAY) > 30) terminal("ineligible", "window.expired");
  else rules.push("window.ok");
  if (condition === "unknown") {
    terminal("needs_information", "condition.unknown");
    missing.push("condition");
  }
  if (condition === "worn" && !isDefect) terminal("ineligible", "condition.worn");
  if (line.unit_paid_cents * qty > 25000) terminal("needs_review", "authority.refund_limit");
  return {
    decision,
    rule_ids: rules,
    missing_fields: missing,
    allowed_option_types: decision === "eligible" ? (reason === "FIT_SIZE" ? [...EXCHANGES, ...RETURNS] : RETURNS) : [],
    policy_version: POLICY_VERSION,
  };
}

const styleToken = (style: string): string => style.replace("DEMO-STYLE-", "");

export function sizeRun(style: string): { token: string; color: string } | null {
  const token = styleToken(style);
  if (token === HERO.token) return { token, color: HERO.color };
  if (token === CONTRAST.token) return { token, color: CONTRAST.color };
  return null;
}

export function condition(type: string): string[] {
  switch (type) {
    case "exchange_store_pickup":
      return ["Bring the original pair, unworn", "Collect within 7 days of the ready date"];
    case "exchange_ship_from_store":
    case "exchange_ship_from_dc":
      return ["Send back the original pair, unworn, in its box", "Free shipping both ways"];
    case "return_in_store":
      return ["Bring the original pair, unworn", "Refund at drop-off to the original payment method"];
    default:
      return ["Standard shipping fee of CAD 5.95 is taken from the refund", "Refund follows once ALDO receives the parcel"];
  }
}

export interface Candidates {
  options: QuoteOption[];
  replacementSize: string | null;
  replacementSku: string | null;
  dcAtp: number;
  reasonNoExchange: string | null;
  otherSizes: { size: string; total_atp: number }[];
}

/** Server-side option candidates, following contracts.md A.9 (stable order and rules). */
export function candidatesFor(
  customerId: string,
  line: MockLine,
  policy: PolicyOutcome,
  direction: string,
  deadline: string | null,
): Candidates {
  const home = persona(customerId)?.home_region ?? "QC";
  const stores = LOCATIONS.filter((l) => l.type === "STORE").sort((a, b) => (a.location_id < b.location_id ? -1 : 1));
  const dc = LOCATIONS.find((l) => l.type === "DC") as MockLocation;
  const out: Omit<QuoteOption, "option_id">[] = [];
  const allowed = new Set(policy.allowed_option_types);
  const qty = 1;

  const run = sizeRun(line.style);
  let replacementSize: string | null = null;
  let replacementSku: string | null = null;
  let reasonNoExchange: string | null = null;
  let dcAtp = 0;
  const hasExchange = EXCHANGES.some((t) => allowed.has(t));
  if (!hasExchange) reasonNoExchange = "NOT_FIT_REASON";
  else if (direction === "unknown") reasonNoExchange = "NO_SUCH_SIZE";
  else if (!run) reasonNoExchange = "NO_SUCH_SIZE";
  else {
    const target = Number(line.size) + (direction === "too_small" ? 0.5 : -0.5);
    replacementSize = sizeStr(target);
    replacementSku = skuFor(run.token, run.color, target);
    const known = [...state.inventory.keys()].some((k) => k.startsWith(`${replacementSku}|`));
    if (!known) {
      reasonNoExchange = "NO_SUCH_SIZE";
      replacementSku = null;
    }
  }

  const eta = (d: number) => addDays(DEMO_TODAY, d);
  const meets = (etaDate: string): boolean | null => (deadline ? etaDate <= deadline : null);
  const exchange = (type: string, loc: MockLocation, days: number) => {
    const etaDate = eta(days);
    const have = atp(replacementSku as string, loc.location_id);
    out.push({
      type,
      sku: replacementSku as string,
      replacement_size: replacementSize,
      location_id: loc.location_id,
      location_name: loc.name,
      qty,
      refund_cents: 0,
      fee_cents: 0,
      currency: "CAD",
      eta_date: etaDate,
      eta_text: weekdayText(etaDate),
      meets_deadline: meets(etaDate),
      conditions: condition(type),
      evidence: { atp: have, inventory_version: 1, rate_id: type === "exchange_store_pickup" ? "DEMO-RATE-XPICKUP" : "DEMO-RATE-XSHIP" },
      source_refs: [`INV#${replacementSku}/LOC#${loc.location_id}`, `DEMO-POLICY-CA@${POLICY_VERSION}`],
      provenance: "synthetic",
    });
  };

  if (replacementSku) {
    dcAtp = atp(replacementSku, dc.location_id);
    if (allowed.has("exchange_ship_from_dc") && dcAtp >= qty) exchange("exchange_ship_from_dc", dc, dc.ship_days);
    if (allowed.has("exchange_ship_from_store")) {
      const withStock = stores
        .filter((s) => atp(replacementSku as string, s.location_id) >= qty)
        .sort((a, b) => a.ship_days - b.ship_days || Number(b.region === home) - Number(a.region === home) || (a.location_id < b.location_id ? -1 : 1));
      if (withStock[0]) exchange("exchange_ship_from_store", withStock[0], withStock[0].ship_days);
    }
    if (allowed.has("exchange_store_pickup")) {
      for (const s of stores.filter((x) => x.region === home && atp(replacementSku as string, x.location_id) >= qty)) {
        exchange("exchange_store_pickup", s, s.pickup_ready_days ?? 1);
      }
    }
  }

  const gross = line.unit_paid_cents * qty;
  const returnStore = stores.find((s) => s.region === home) ?? stores[0];
  if (allowed.has("return_in_store")) {
    out.push({
      type: "return_in_store",
      sku: line.sku,
      replacement_size: null,
      location_id: returnStore.location_id,
      location_name: returnStore.name,
      qty,
      refund_cents: gross,
      fee_cents: 0,
      currency: "CAD",
      eta_date: eta(0),
      eta_text: weekdayText(eta(0)),
      meets_deadline: null,
      conditions: condition("return_in_store"),
      evidence: { rate_id: "DEMO-RATE-INSTORE" },
      source_refs: ["DEMO-LOGISTICS-CA", `DEMO-POLICY-CA@${POLICY_VERSION}`],
      provenance: "synthetic",
    });
  }
  if (allowed.has("return_mail")) {
    out.push({
      type: "return_mail",
      sku: line.sku,
      replacement_size: null,
      location_id: dc.location_id,
      location_name: dc.name,
      qty,
      refund_cents: gross - 595,
      fee_cents: 595,
      currency: "CAD",
      eta_date: eta(10),
      eta_text: weekdayText(eta(10)),
      meets_deadline: null,
      conditions: condition("return_mail"),
      evidence: { rate_id: "DEMO-RATE-MAIL" },
      source_refs: ["DEMO-LOGISTICS-CA", `DEMO-POLICY-CA@${POLICY_VERSION}`],
      provenance: "synthetic",
    });
  }

  const otherSizes: Candidates["otherSizes"] = [];
  return {
    options: out.map((o, i) => ({ ...o, option_id: `OPT-${i + 1}` })),
    replacementSize,
    replacementSku,
    dcAtp,
    reasonNoExchange,
    otherSizes,
  };
}

/** The mock agent's ranking: exchanges that meet the deadline first (preferring Montreal), then other exchanges, then returns. */
export function rankForQuote(options: QuoteOption[]): QuoteOption[] {
  const exchanges = options
    .filter((o) => o.type.startsWith("exchange"))
    .sort((a, b) => {
      const am = a.meets_deadline === true ? 0 : 1;
      const bm = b.meets_deadline === true ? 0 : 1;
      if (am !== bm) return am - bm;
      const ap = a.type === "exchange_store_pickup" ? 0 : 1;
      const bp = b.type === "exchange_store_pickup" ? 0 : 1;
      if (ap !== bp) return ap - bp;
      const al = a.location_id === "DEMO-STORE-MTL-01" ? 0 : 1;
      const bl = b.location_id === "DEMO-STORE-MTL-01" ? 0 : 1;
      if (al !== bl) return al - bl;
      return a.eta_date < b.eta_date ? -1 : a.eta_date > b.eta_date ? 1 : 0;
    })
    .slice(0, 3);
  const returns = options.filter((o) => o.type.startsWith("return"));
  return [...exchanges, ...returns];
}

export function canonicalJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(",")}]`;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v);
}

export function payloadHash(quoteId: string, o: QuoteOption, policyVersion: string): string {
  return createHash("sha256")
    .update(
      canonicalJson({
        quote_id: quoteId,
        option_id: o.option_id,
        type: o.type,
        sku: o.sku,
        location: o.location_id,
        qty: o.qty,
        refund_cents: o.refund_cents,
        fee_cents: o.fee_cents,
        currency: o.currency,
        policy_version: policyVersion,
      }),
    )
    .digest("hex");
}

/** Quote status as the API reports it: "expired" is computed on read. */
export function quoteStatus(q: Quote, t = now()): string {
  if (q.stored_status === "open" && t >= q.expiresAtMs) return "expired";
  return q.stored_status;
}

export function nextStepText(o: {
  type: string;
  location_name: string;
  replacement_size: string | null;
  replacement_color?: string | null;
  eta_date: string;
  fee_cents: number;
}): string {
  const day = weekdayText(o.eta_date);
  const what = `${o.replacement_color ? `${o.replacement_color.toLowerCase()} ` : "size "}${o.replacement_size}`;
  switch (o.type) {
    case "exchange_store_pickup":
      return o.eta_date === DEMO_TODAY
        ? `Pick up ${what} at ${o.location_name} today. Bring the original pair, unworn.`
        : `Pick up ${what} at ${o.location_name} from ${day}. Bring the original pair, unworn.`;
    case "exchange_ship_from_store":
    case "exchange_ship_from_dc":
      return `${what} ships from ${o.location_name} and should arrive ${day}. Send back the original pair, unworn, in its box.`;
    case "return_in_store":
      return `Bring the original pair, unworn, to ${o.location_name}. Your refund is issued at drop-off.`;
    default:
      return o.fee_cents > 0
        ? `A return label has been created. Pack the items and drop the parcel at any carrier point; the CAD 5.95 shipping fee comes out of your refund, which follows once ALDO receives it.`
        : `A return label has been created. Pack the items and drop the parcel at any carrier point; shipping is free and your refund follows once ALDO receives it.`;
  }
}

export function purchaseNextStep(o: Pick<OfferedOption, "type" | "size" | "location_name" | "eta_text">): string {
  return o.type === "store_pickup"
    ? `Pick up size ${o.size} at ${o.location_name} from ${o.eta_text}.`
    : `Size ${o.size} ships from ${o.location_name} and should arrive by ${o.eta_text}.`;
}

/** The ONLY place an agent-channel order is created: the owner's confirm click (or a mock control standing in for another client). */
export function placeOrder(sr: SizeRequest, opt: OfferedOption, approvedBy = "owner_click") {
  const orderId = `DEMO-A2A-${hex(6)}`;
  state.orders.push({
    order_id: orderId,
    customer_id: sr.customer_id,
    banner: "ALDO_CA",
    currency: "CAD",
    channel: "agent",
    ordered_at: DEMO_TODAY,
    delivered_at: null,
    source: "synthetic",
    lines: [
      {
        line_id: `${orderId}-L1`,
        sku: opt.sku,
        product_name: sr.product_name,
        style: sr.style_id,
        size: opt.size,
        color: sr.color,
        purchased_qty: 1,
        returned_or_reserved_qty: 0,
        unit_paid_cents: sr.price_cents,
        final_sale: false,
      },
    ],
  });
  takeStock(opt.sku, opt.location_id, 1);
  const nextStep = purchaseNextStep(opt);
  sr.status = "decided";
  sr.order_id = orderId;
  sr.chosen_option_id = opt.option_id;
  sr.next_step = nextStep;
  const fulfillment = { type: opt.type, location_id: opt.location_id, location_name: opt.location_name, eta_date: opt.eta_date };
  pushA2a(sr.customer_id, "size_confirmed", "aldo_gateway", {
    size_request_id: sr.size_request_id,
    order_id: orderId,
    status: "placed_simulated",
    size: opt.size,
    location_name: opt.location_name,
    eta_date: opt.eta_date,
    price_cents: sr.price_cents,
    fulfillment,
    approved_by: approvedBy,
  });
  return {
    order_id: orderId,
    status: "placed_simulated",
    line: { sku: opt.sku, size: opt.size, unit_paid_cents: sr.price_cents },
    fulfillment,
    next_step: nextStep,
    replayed: false,
  };
}

export { addDays, daysBetween, locationById, weekdayText, DEMO_TODAY, POLICY_VERSION };
