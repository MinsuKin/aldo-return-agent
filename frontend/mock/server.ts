// Mock API for the Agentic Returns Gateway UI (no dependencies). Emulates contracts.md section (c)
// and the pivot addendum routes, with scripted agent runs delivered over time so the UI can be
// verified without the backend.
//
//   npm run mock                       # http://127.0.0.1:3001, vite proxies /api to it
//   MOCK_TIME_SCALE=0.3 npm run mock   # faster scripted runs
//   MOCK_VARIANT=default npm run mock  # start with DC stock (default is dc_stockout, the hero demo)
//
// Not part of the product: nothing here is deployed.

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createHmac, timingSafeEqual } from "node:crypto";
import { PERSONAS, VARIANTS, DEMO_TODAY, locationById, type Variant } from "./data.ts";
import { buildAldoRun, buildApprovalRun, buildRequoteRun } from "./agents.ts";
import { buildPersonalRun } from "./personal.ts";
import {
  SCALE,
  atp,
  flushOutbox,
  hex,
  iso,
  materialize,
  memoryFor,
  newId,
  now,
  orderById,
  ordersOf,
  persona,
  placeOrder,
  pushA2a,
  quoteStatus,
  resetState,
  runDone,
  runStatus,
  state,
  type Approval,
  type Conv,
  type Quote,
  type Run,
  type RunKind,
  type Script,
  type SizeRequest,
  writebackDelay,
} from "./state.ts";

const PORT = Number(process.env.MOCK_PORT ?? "3001");
const ADMIN_SECRET = process.env.MOCK_ADMIN_SECRET ?? "mock-admin";
const TOKEN_KEY = process.env.MOCK_TOKEN_KEY ?? `mock-key-${hex(16)}`;
const TOKEN_TTL_MS = 8 * 60 * 60 * 1000;

class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  constructor(status: number, code: string, message: string, retryable = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

interface Ctx {
  params: Record<string, string>;
  query: URLSearchParams;
  body: Record<string, unknown>;
  customerId: string;
  headers: IncomingMessage["headers"];
}
interface Result {
  status: number;
  body: unknown;
}
type Handler = (c: Ctx) => Result | Promise<Result>;
interface Route {
  method: string;
  re: RegExp;
  keys: string[];
  auth: boolean;
  handler: Handler;
}

const routes: Route[] = [];
function route(method: string, pattern: string, auth: boolean, handler: Handler): void {
  const keys: string[] = [];
  const re = new RegExp(`^${pattern.replace(/:([a-z_]+)/g, (_m, k: string) => (keys.push(k), "([^/]+)"))}$`);
  routes.push({ method, re, keys, auth, handler });
}

// ---- Tokens: b64url(payload).b64url(hmac) -----------------------------------------------------

const b64 = (b: Buffer | string): string => Buffer.from(b).toString("base64url");
const sign = (data: string): string => b64(createHmac("sha256", TOKEN_KEY).update(data).digest());

function mintToken(customerId: string): { token: string; expires_at: string } {
  const exp = now() + TOKEN_TTL_MS;
  const payload = b64(JSON.stringify({ c: customerId, exp, scope: "session" }));
  return { token: `${payload}.${sign(payload)}`, expires_at: iso(exp) };
}

function customerFromHeader(header: string | undefined): string {
  const m = /^Bearer (.+)$/.exec(header ?? "");
  if (!m) throw new ApiError(401, "NOT_AUTHENTICATED", "Missing or malformed token.");
  const [payload, sig] = m[1].split(".");
  if (!payload || !sig) throw new ApiError(401, "NOT_AUTHENTICATED", "Missing or malformed token.");
  const expected = Buffer.from(sign(payload));
  const given = Buffer.from(sig);
  if (expected.length !== given.length || !timingSafeEqual(expected, given)) throw new ApiError(401, "NOT_AUTHENTICATED", "Invalid token signature.");
  const data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as { c: string; exp: number };
  if (data.exp <= now()) throw new ApiError(401, "NOT_AUTHENTICATED", "Token expired.");
  return data.c;
}

// ---- Helpers --------------------------------------------------------------------------------

const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

function requireConv(customerId: string, convId: string, agent: Conv["agent"]): Conv {
  const conv = state.convs.get(convId);
  if (!conv || conv.agent !== agent) throw new ApiError(404, "NOT_FOUND", "Conversation not found.");
  if (conv.customer_id !== customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Conversation not found for this customer.");
  return conv;
}

function requireQuote(customerId: string, quoteId: string): Quote {
  const q = state.quotes.get(quoteId);
  if (!q) throw new ApiError(404, "NOT_FOUND", "Quote not found.");
  if (q.customer_id !== customerId) {
    state.auditCount += 1;
    throw new ApiError(403, "NOT_AUTHORIZED", "Quote not found for this customer.");
  }
  return q;
}

function activeRunOf(conv: Conv): Run | undefined {
  const run = conv.active_run_id ? state.runs.get(conv.active_run_id) : undefined;
  return run && !runDone(run) ? run : undefined;
}

function startRun(conv: Conv, customerId: string, runId: string, kind: RunKind, s: Script): Run {
  const run: Run = { run_id: runId, customer_id: customerId, conv_id: conv.conv_id, kind, startedAt: now(), events: s.events };
  state.runs.set(runId, run);
  conv.active_run_id = runId;
  return run;
}

function orderView(o: ReturnType<typeof orderById>) {
  if (!o) return null;
  return {
    order_id: o.order_id,
    banner: o.banner,
    currency: o.currency,
    channel: o.channel,
    ordered_at: o.ordered_at,
    delivered_at: o.delivered_at,
    source: o.source,
    lines: o.lines.map((l) => ({
      line_id: l.line_id,
      sku: l.sku,
      product_name: l.product_name,
      style: l.style,
      size: l.size,
      color: l.color,
      purchased_qty: l.purchased_qty,
      returned_or_reserved_qty: l.returned_or_reserved_qty,
      unit_paid_cents: l.unit_paid_cents,
      final_sale: l.final_sale,
      currency: "CAD",
      source: "synthetic",
    })),
  };
}

function quoteView(q: Quote) {
  return {
    quote_id: q.quote_id,
    order_id: q.order_id,
    line_id: q.line_id,
    conv_id: q.conv_id,
    run_id: q.run_id,
    policy_check_id: q.policy_check_id,
    candidate_set_id: q.candidate_set_id,
    policy_version: q.policy_version,
    currency: q.currency,
    created_at: q.created_at,
    expires_at: iso(q.expiresAtMs),
    status: quoteStatus(q),
    options: q.options,
    approval: q.approval,
    inventory_versions: {},
  };
}

function validateText(body: Record<string, unknown>): string {
  const text = str(body.text)?.trim() ?? "";
  if (text.length < 1 || text.length > 1000) throw new ApiError(422, "VALIDATION_ERROR", "text must be 1 to 1000 characters.");
  if (text.toLowerCase().startsWith("[server]")) throw new ApiError(422, "VALIDATION_ERROR", "Messages may not start with [server].");
  return text;
}

function serveEvents(run: Run, after: number) {
  materialize();
  const t = now();
  const visible = run.events.filter((e) => run.startedAt + e.at <= t);
  const events = visible
    .filter((e) => e.seq > after)
    .map((e) => ({
      seq: e.seq,
      type: e.type,
      ts: iso(run.startedAt + e.at),
      actor: e.actor ?? (run.kind === "personal" ? "personal_agent" : "aldo_agent"),
      payload: e.payload,
    }));
  return {
    run_id: run.run_id,
    status: runStatus(run, t),
    events,
    next_after: events.length ? events[events.length - 1].seq : after,
  };
}

// ---- Routes: demo, sessions, reset -------------------------------------------------------------

route("GET", "/api/demo/personas", false, () => ({
  status: 200,
  body: { personas: PERSONAS.map((p) => ({ customer_id: p.customer_id, display_name: p.display_name, description: p.description })) },
}));

route("POST", "/api/demo/sessions", false, (c) => {
  const p = persona(str(c.body.customer_id) ?? "");
  if (!p) throw new ApiError(422, "VALIDATION_ERROR", "Unknown persona.");
  return { status: 200, body: { ...mintToken(p.customer_id), customer: { customer_id: p.customer_id, display_name: p.display_name, home_region: p.home_region } } };
});

route("POST", "/api/demo/reset", false, (c) => {
  const given = Buffer.from(String(c.headers["x-demo-admin"] ?? ""));
  const want = Buffer.from(ADMIN_SECRET);
  if (given.length !== want.length || !timingSafeEqual(given, want)) throw new ApiError(401, "NOT_AUTHENTICATED", "Admin secret required.");
  const variant = (str(c.body.variant) ?? "default") as Variant;
  if (!VARIANTS.includes(variant)) throw new ApiError(422, "VALIDATION_ERROR", "Unknown variant.");
  const counts = resetState(variant);
  return { status: 200, body: { ok: true, variant, ...counts } };
});

route("GET", "/api/impact", false, () => ({ status: 200, body: { note: "mock: the UI no longer renders the impact model (it moved to a slide)" } }));

route("POST", "/api/demo/simulate-delivery", true, (c) => {
  const order = orderById(str(c.body.order_id) ?? "");
  if (!order) throw new ApiError(404, "ORDER_NOT_FOUND", "Order not found.");
  if (order.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Order not found for this customer.");
  if (order.channel !== "agent") throw new ApiError(422, "VALIDATION_ERROR", "Only agent-placed orders can be marked delivered.");
  order.delivered_at = DEMO_TODAY;
  return { status: 200, body: { ok: true, order_id: order.order_id, delivered_at: order.delivered_at } };
});

route("POST", "/api/demo/simulate-pickup", true, (c) => {
  materialize();
  const txn = state.txns.get(str(c.body.transaction_id) ?? "");
  if (!txn) throw new ApiError(404, "NOT_FOUND", "Transaction not found.");
  if (txn.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Transaction not found for this customer.");
  if (!txn.update_id) throw new ApiError(422, "VALIDATION_ERROR", "Only exchanges can be picked up.");
  // The pickup confirmation follows the exchange's own update; make sure that one has been delivered.
  const saved = writebackDelay.ms;
  writebackDelay.ms = 0;
  flushOutbox(c.customerId);
  writebackDelay.ms = saved;
  const update = (state.memoryUpdates.get(c.customerId) ?? []).find((u) => u.update_id === txn.update_id);
  if (!update) throw new ApiError(422, "VALIDATION_ERROR", "Only exchanges can be picked up.");
  update.outcome = "exchange_completed_kept";
  update.provisional = false;
  pushA2a(c.customerId, "memory_updated", "personal_agent", {
    update_id: update.update_id,
    style_id: update.style_id,
    product_name: update.product_name,
    original_size: update.original_size,
    new_size: update.new_size,
    reason: update.reason,
    outcome: "exchange_completed_kept",
    provisional: false,
  });
  return { status: 200, body: { ok: true, transaction_id: txn.transaction_id, update_id: update.update_id } };
});

// ---- Routes: orders and ALDO conversations -----------------------------------------------------

route("GET", "/api/orders", true, (c) => {
  materialize();
  return { status: 200, body: { orders: ordersOf(c.customerId).map(orderView) } };
});

route("POST", "/api/conversations", true, (c) => {
  const orderId = str(c.body.order_id) ?? null;
  if (orderId) {
    const o = orderById(orderId);
    if (!o) throw new ApiError(404, "ORDER_NOT_FOUND", "Order not found.");
    if (o.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Order not found for this customer.");
  }
  const conv: Conv = { conv_id: newId("CONV-", 10), customer_id: c.customerId, agent: "aldo", order_id: orderId, active_run_id: null, memo: {} };
  state.convs.set(conv.conv_id, conv);
  return { status: 201, body: { conv_id: conv.conv_id } };
});

route("POST", "/api/conversations/:conv/messages", true, (c) => {
  const conv = requireConv(c.customerId, c.params.conv, "aldo");
  const text = validateText(c.body);
  const orderId = str(c.body.order_id) ?? null;
  if (orderId) {
    const o = orderById(orderId);
    if (!o) throw new ApiError(404, "ORDER_NOT_FOUND", "Order not found.");
    if (o.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Order not found for this customer.");
  }
  if (activeRunOf(conv)) throw new ApiError(409, "RUN_IN_PROGRESS", "A run is already active for this conversation.", true);
  const runId = newId("RUN-", 10);
  startRun(conv, c.customerId, runId, "customer", buildAldoRun({ customerId: c.customerId, conv, runId, message: text, bodyOrderId: orderId }));
  return { status: 202, body: { run_id: runId, conv_id: conv.conv_id } };
});

route("POST", "/api/conversations/:conv/requote", true, (c) => {
  const conv = requireConv(c.customerId, c.params.conv, "aldo");
  const quote = requireQuote(c.customerId, str(c.body.quote_id) ?? "");
  if (quote.conv_id !== conv.conv_id) throw new ApiError(403, "NOT_AUTHORIZED", "Quote does not belong to this conversation.");
  if (quoteStatus(quote) !== "expired") throw new ApiError(422, "VALIDATION_ERROR", "Only expired quotes can be refreshed.");
  if (activeRunOf(conv)) throw new ApiError(409, "RUN_IN_PROGRESS", "A run is already active for this conversation.", true);
  const runId = newId("RUN-", 10);
  startRun(conv, c.customerId, runId, "requote", buildRequoteRun({ customerId: c.customerId, conv, runId, quote }));
  return { status: 202, body: { run_id: runId, conv_id: conv.conv_id } };
});

route("GET", "/api/runs/:run/events", true, (c) => {
  const run = state.runs.get(c.params.run);
  if (!run) throw new ApiError(404, "NOT_FOUND", "Run not found.");
  if (run.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Run not found for this customer.");
  const after = Number(c.query.get("after") ?? "0");
  if (!Number.isInteger(after) || after < 0) throw new ApiError(422, "VALIDATION_ERROR", "after must be an integer >= 0.");
  return { status: 200, body: serveEvents(run, after) };
});

route("GET", "/api/quotes/:qid", true, (c) => {
  materialize();
  return { status: 200, body: quoteView(requireQuote(c.customerId, c.params.qid)) };
});

route("POST", "/api/quotes/:qid/approve", true, (c) => {
  materialize();
  const quote = requireQuote(c.customerId, c.params.qid);
  const optionId = str(c.body.option_id) ?? "";
  const hash = str(c.body.payload_hash) ?? "";
  const option = quote.options.find((o) => o.option_id === optionId);
  if (!option || option.payload_hash !== hash) throw new ApiError(422, "VALIDATION_ERROR", "Option or payload hash does not match this quote.");

  if (quote.approval) {
    if (quote.approval.option_id !== optionId) throw new ApiError(409, "IDEMPOTENCY_CONFLICT", "A different option was already approved on this quote.");
    return { status: 200, body: { approval_id: quote.approval.approval_id, run_id: quote.approval.run_id, quote_id: quote.quote_id, option_id: optionId, replayed: true } };
  }
  if (quoteStatus(quote) !== "open") throw new ApiError(409, "QUOTE_EXPIRED", "This quote has expired or was replaced. Ask for fresh options.");
  const conv = state.convs.get(quote.conv_id) as Conv;
  if (activeRunOf(conv)) throw new ApiError(409, "RUN_IN_PROGRESS", "The assistant is still working on this conversation.", true);

  const runId = newId("RUN-", 10);
  const approval: Approval = {
    approval_id: newId("APR-", 8),
    quote_id: quote.quote_id,
    option_id: optionId,
    payload_hash: hash,
    run_id: runId,
    approved_at: iso(now()),
    consumed_txn_id: null,
  };
  quote.approval = approval;
  startRun(conv, c.customerId, runId, "approval", buildApprovalRun({ customerId: c.customerId, conv, runId, quote, approval }));
  return { status: 202, body: { approval_id: approval.approval_id, run_id: runId, quote_id: quote.quote_id, option_id: optionId, replayed: false } };
});

route("GET", "/api/transactions/:tid", true, (c) => {
  materialize();
  const txn = state.txns.get(c.params.tid);
  if (!txn) throw new ApiError(404, "NOT_FOUND", "Transaction not found.");
  if (txn.customer_id !== c.customerId) throw new ApiError(403, "NOT_AUTHORIZED", "Transaction not found for this customer.");
  const line = orderById(txn.order_id)?.lines.find((l) => l.line_id === txn.line_id);
  const { customer_id: _omit, update_id: _omit2, ...view } = txn;
  void _omit;
  void _omit2;
  return {
    status: 200,
    body: {
      ...view,
      reservation: {
        line_returned_or_reserved_qty: line?.returned_or_reserved_qty ?? 0,
        line_purchased_qty: line?.purchased_qty ?? 0,
        location_atp: txn.option_type.startsWith("exchange") ? atp(txn.sku, txn.location_id) : null,
      },
      location_name: locationById(txn.location_id).name,
    },
  };
});

route("GET", "/api/cases", true, (c) => {
  materialize();
  return { status: 200, body: { cases: state.cases.filter((k) => k.customer_id === c.customerId).map(({ customer_id: _c, ...rest }) => (void _c, rest)) } };
});

// ---- Routes: personal agent and A2A feed ----------------------------------------------------------

route("POST", "/api/personal-agent/conversations", true, (c) => {
  const conv: Conv = { conv_id: newId("PCONV-", 10), customer_id: c.customerId, agent: "personal", order_id: null, active_run_id: null, memo: {} };
  state.convs.set(conv.conv_id, conv);
  return { status: 201, body: { conv_id: conv.conv_id } };
});

route("POST", "/api/personal-agent/conversations/:conv/messages", true, (c) => {
  const conv = requireConv(c.customerId, c.params.conv, "personal");
  const text = validateText(c.body);
  if (activeRunOf(conv)) throw new ApiError(409, "RUN_IN_PROGRESS", "Your personal agent is still working.", true);
  const runId = newId("PRUN-", 10);
  startRun(conv, c.customerId, runId, "personal", buildPersonalRun({ customerId: c.customerId, conv, runId, message: text }));
  return { status: 202, body: { run_id: runId, conv_id: conv.conv_id } };
});

route("GET", "/api/personal-agent/memory", true, (c) => {
  materialize();
  // Like the real API: deliver any due write-backs from ALDO (the outbox) before reading memory.
  flushOutbox(c.customerId);
  return { status: 200, body: memoryFor(c.customerId) };
});

route("GET", "/api/a2a/events", true, (c) => {
  materialize();
  const after = Number(c.query.get("after") ?? "0");
  if (!Number.isInteger(after) || after < 0) throw new ApiError(422, "VALIDATION_ERROR", "after must be an integer >= 0.");
  const all = state.a2a.get(c.customerId) ?? [];
  const events = all.filter((e) => e.seq > after);
  return { status: 200, body: { events, next_after: events.length ? events[events.length - 1].seq : after } };
});

// ---- Routes: purchase proposals (ALDO bot proposes, only the owner's click orders) ---------------------

function requestFor(customerId: string, id: string): SizeRequest {
  const sr = state.sizeRequests.get(id);
  if (!sr) throw new ApiError(404, "NOT_FOUND", "Size request not found.");
  if (sr.customer_id !== customerId) {
    state.auditCount += 1;
    throw new ApiError(403, "NOT_AUTHORIZED", "Size request not found for this customer.");
  }
  return sr;
}

function purchaseView(sr: SizeRequest) {
  const shown = sr.offered.find((o) => o.option_id === (sr.chosen_option_id ?? sr.proposed_option_id));
  return {
    size_request_id: sr.size_request_id,
    status: sr.status,
    product_name: sr.product_name,
    color: sr.color,
    size: shown?.size ?? null,
    option_id: shown?.option_id ?? null,
    option_type: shown?.type ?? null,
    location_id: shown?.location_id ?? null,
    location_name: shown?.location_name ?? null,
    eta_date: shown?.eta_date ?? null,
    eta_text: shown?.eta_text ?? null,
    meets_deadline: shown?.meets_deadline ?? null,
    price_cents: sr.price_cents,
    currency: sr.currency,
    fit_signal: sr.fit_signal,
    rationale_public: sr.rationale_public,
    confidence: sr.confidence,
    expires_at: iso(sr.expiresAtMs),
    order_id: sr.order_id,
    next_step: sr.next_step,
  };
}

route("GET", "/api/purchases/:id", true, (c) => {
  materialize();
  return { status: 200, body: purchaseView(requestFor(c.customerId, c.params.id)) };
});

route("POST", "/api/purchases/:id/confirm", true, (c) => {
  materialize();
  const sr = requestFor(c.customerId, c.params.id);
  const optionId = str(c.body.option_id) ?? "";
  const option = sr.offered.find((o) => o.option_id === optionId);
  if (!option) throw new ApiError(422, "INVALID_SIZE_CHOICE", "That option was not offered for this request.");
  if (sr.status === "decided") {
    if (sr.chosen_option_id !== optionId) throw new ApiError(409, "IDEMPOTENCY_CONFLICT", "A different option was already confirmed for this request.");
    const done = sr.offered.find((o) => o.option_id === sr.chosen_option_id) ?? option;
    return {
      status: 200,
      body: {
        order_id: sr.order_id,
        status: "placed_simulated",
        line: { sku: done.sku, size: done.size, unit_paid_cents: sr.price_cents },
        fulfillment: { type: done.type, location_id: done.location_id, location_name: done.location_name, eta_date: done.eta_date },
        next_step: sr.next_step,
        replayed: true,
      },
    };
  }
  if (sr.status === "open") throw new ApiError(409, "APPROVAL_REQUIRED", "ALDO bot has not made a proposal for this request yet.");
  if (sr.proposed_option_id !== optionId) throw new ApiError(409, "IDEMPOTENCY_CONFLICT", "That is not the option ALDO bot proposed.");
  if (now() >= sr.expiresAtMs) throw new ApiError(409, "REQUEST_EXPIRED", "This proposal has expired.");
  if (atp(option.sku, option.location_id) < 1) throw new ApiError(409, "STOCK_CHANGED", "That option is no longer in stock.");
  return { status: 200, body: placeOrder(sr, option, "owner_click") };
});

// ---- Mock-only controls (not part of the real API) ------------------------------------------------

route("POST", "/__mock/expire-quote", false, (c) => {
  const q = state.quotes.get(str(c.body.quote_id) ?? "");
  if (!q) throw new ApiError(404, "NOT_FOUND", "Quote not found.");
  q.expiresAtMs = now() - 1000;
  return { status: 200, body: { ok: true, quote_id: q.quote_id } };
});

route("POST", "/__mock/approve-other", false, (c) => {
  // Approve an option directly (as another client would) so a UI click hits IDEMPOTENCY_CONFLICT.
  const q = state.quotes.get(str(c.body.quote_id) ?? "");
  const o = q?.options.find((x) => x.option_id === str(c.body.option_id));
  if (!q || !o) throw new ApiError(404, "NOT_FOUND", "Quote or option not found.");
  if (!q.approval) {
    q.approval = {
      approval_id: newId("APR-", 8),
      quote_id: q.quote_id,
      option_id: o.option_id,
      payload_hash: o.payload_hash ?? "",
      run_id: newId("RUN-", 10),
      approved_at: iso(now()),
      consumed_txn_id: null,
    };
  }
  return { status: 200, body: { ok: true } };
});

route("POST", "/__mock/writeback-delay", false, (c) => {
  // Simulates a slow outbox: a memory read only delivers write-backs older than this many milliseconds.
  const ms = typeof c.body.ms === "number" ? c.body.ms : NaN;
  if (!Number.isFinite(ms) || ms < 0) throw new ApiError(422, "VALIDATION_ERROR", "ms must be a number >= 0.");
  writebackDelay.ms = ms;
  return { status: 200, body: { ok: true, ms } };
});

route("POST", "/__mock/expire-purchase", false, (c) => {
  const sr = state.sizeRequests.get(str(c.body.size_request_id) ?? "");
  if (!sr) throw new ApiError(404, "NOT_FOUND", "Size request not found.");
  sr.expiresAtMs = now() - 1000;
  return { status: 200, body: { ok: true } };
});

route("POST", "/__mock/set-atp", false, (c) => {
  const sku = str(c.body.sku);
  const loc = str(c.body.location_id);
  if (!sku || !loc || typeof c.body.atp !== "number") throw new ApiError(422, "VALIDATION_ERROR", "sku, location_id and numeric atp are required.");
  state.inventory.set(`${sku}|${loc}`, c.body.atp);
  return { status: 200, body: { ok: true } };
});

route("POST", "/__mock/confirm-other", false, (c) => {
  // Another client confirms a different option on the same request first, so a UI click hits IDEMPOTENCY_CONFLICT.
  const sr = state.sizeRequests.get(str(c.body.size_request_id) ?? "");
  if (!sr) throw new ApiError(404, "NOT_FOUND", "Size request not found.");
  const other = sr.offered.find((o) => o.option_id !== sr.proposed_option_id);
  if (!other) throw new ApiError(422, "VALIDATION_ERROR", "Only one option was offered.");
  if (sr.status !== "decided") placeOrder(sr, other, "owner_click");
  return { status: 200, body: { ok: true, option_id: other.option_id } };
});

route("GET", "/__mock/health", false, () => ({
  status: 200,
  body: { ok: true, variant: state.variant, time_scale: SCALE, runs: state.runs.size, quotes: state.quotes.size },
}));

// ---- Server --------------------------------------------------------------------------------------

async function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > 100_000) throw new ApiError(413, "VALIDATION_ERROR", "Body too large.");
    chunks.push(chunk as Buffer);
  }
  if (chunks.length === 0) return {};
  try {
    const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
  } catch {
    throw new ApiError(422, "VALIDATION_ERROR", "Body must be valid JSON.");
  }
}

function send(res: ServerResponse, status: number, body: unknown, requestId: string): void {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
    "x-request-id": requestId,
    "content-length": Buffer.byteLength(text),
  });
  res.end(text);
}

const server = createServer((req, res) => {
  const requestId = newId("req-", 8);
  void (async () => {
    const url = new URL(req.url ?? "/", "http://mock.local");
    let status = 500;
    try {
      const method = req.method ?? "GET";
      let matched: { r: Route; params: Record<string, string> } | undefined;
      let pathKnown = false;
      for (const r of routes) {
        const m = r.re.exec(url.pathname);
        if (!m) continue;
        pathKnown = true;
        if (r.method !== method) continue;
        matched = { r, params: Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])])) };
        break;
      }
      if (!matched) {
        throw new ApiError(pathKnown ? 405 : 404, pathKnown ? "VALIDATION_ERROR" : "NOT_FOUND", pathKnown ? "Method not allowed." : "Route not found.");
      }
      const customerId = matched.r.auth ? customerFromHeader(req.headers.authorization) : "";
      const body = method === "POST" ? await readBody(req) : {};
      const result = await matched.r.handler({ params: matched.params, query: url.searchParams, body, customerId, headers: req.headers });
      status = result.status;
      send(res, status, result.body, requestId);
    } catch (e) {
      if (e instanceof ApiError) {
        status = e.status;
        send(res, status, { error: { code: e.code, message: e.message, retryable: e.retryable, request_id: requestId } }, requestId);
      } else {
        console.error("[mock] unexpected error", e);
        status = 500;
        send(res, 500, { error: { code: "INTERNAL_ERROR", message: "Unexpected mock error.", retryable: false, request_id: requestId } }, requestId);
      }
    }
    if (process.env.MOCK_QUIET !== "1") console.log(`[mock] ${req.method} ${url.pathname} -> ${status}`);
  })();
});

resetState(state.variant);
server.listen(PORT, "127.0.0.1", () => {
  console.log(`Mock API on http://127.0.0.1:${PORT} (variant=${state.variant}, time scale=${SCALE}, admin secret=${ADMIN_SECRET}, synthetic data only)`);
});

const shutdown = () => server.close(() => process.exit(0));
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
