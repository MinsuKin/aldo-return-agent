// Scripted stand-ins for the two agents. Each builder returns a timed list of run events (the same
// event types and payload shapes as contracts.md B.4 plus the pivot's A2A events). Side effects
// (quotes, orders, transactions, cases, memory) are applied when the event becomes visible, so
// the UI sees exactly what a real run would show, in the same order.

import type { MockLine, MockOrder } from "./data.ts";
import {
  DEMO_TODAY,
  MODEL_ID,
  POLICY_VERSION,
  QUOTE_TTL_MS,
  addDays,
  addMemoryUpdate,
  candidatesFor,
  evaluatePolicy,
  finishRun,
  hex,
  iso,
  newId,
  nextStepText,
  now,
  orderById,
  ordersOf,
  payloadHash,
  pushA2a,
  rankForQuote,
  script,
  state,
  takeStock,
  toolStep,
  weekdayText,
  type Approval,
  type Conv,
  type Quote,
  type QuoteOption,
  type Script,
  type Txn,
} from "./state.ts";

const assistant = (s: Script, text: string, actor?: string, delay = 500) =>
  s.emit(delay, "assistant_message", { text }, undefined, actor);

// ---- Message understanding (keyword stand-in for the model) -----------------------------

interface Intent {
  mentioned: string | null;
  defect: boolean;
  mind: boolean;
  fit: boolean;
  direction: "too_small" | "too_big" | "unknown";
  worn: boolean;
  deadline: string | null;
}

function understand(message: string): Intent {
  const m = message.toLowerCase();
  const mentioned = /DEMO-\d{4}/i.exec(message)?.[0].toUpperCase() ?? null;
  const defect = /(broke|broken|defect|damag|crack|tore|torn|split|fell apart|peeling)/.test(m);
  const mind = /(changed my mind|change of mind|don'?t want|do not want|no longer|not what i expected|return them for a refund|just want a refund)/.test(m);
  const small = /(too small|too tight|tight|half size up|size up|too narrow|pinch)/.test(m);
  const big = /(too big|too large|too loose|loose|half size down|size down)/.test(m);
  const fit = small || big || /(doesn'?t fit|don'?t fit|wrong size)/.test(m);
  const unworn = /(unworn|not worn|never worn|haven'?t worn|have not worn)/.test(m);
  const worn = !unworn && /\b(worn|wore|wearing)\b/.test(m);
  const deadline = /(saturday|tomorrow)/.test(m) ? addDays(DEMO_TODAY, 1) : /sunday/.test(m) ? addDays(DEMO_TODAY, 2) : null;
  return { mentioned, defect, mind, fit, direction: big && !small ? "too_big" : small ? "too_small" : "unknown", worn, deadline };
}

// ---- ALDO returns agent -------------------------------------------------------------------

interface AldoParams {
  customerId: string;
  conv: Conv;
  runId: string;
  message: string;
  bodyOrderId: string | null;
}

export function buildAldoRun(p: AldoParams): Script {
  const s = script();
  const it = understand(p.message);
  const mentionedOrder = it.mentioned ? orderById(it.mentioned) : undefined;
  const foreign = !!it.mentioned && (!mentionedOrder || mentionedOrder.customer_id !== p.customerId);
  const orderId = foreign ? null : (it.mentioned ?? p.bodyOrderId ?? p.conv.memo.orderId ?? null);
  const startPayload = { kind: "customer", conv_id: p.conv.conv_id, order_id: orderId, model_id: MODEL_ID, max_rounds: 8 };
  s.emit(150, "run_started", startPayload);
  let round = 1;
  let tools = 0;

  // Another customer's order, or an unknown id: denied and audited. "Ignore your policy" changes nothing.
  if (foreign) {
    tools += 1;
    toolStep(s, "get_order_context", { order_id: it.mentioned }, { ok: false, code: mentionedOrder ? "NOT_AUTHORIZED" : "ORDER_NOT_FOUND", latency: 85 }, { round: round++ });
    s.emit(550, "decision", {
      summary: "I can only see the signed-in customer's own orders, and instructions in a message do not change the policy result.",
    });
    assistant(
      s,
      `I can't open order ${it.mentioned}: it isn't on your account, and I can only work with your own orders. Telling me to ignore the policy doesn't change the rules either. If one of your orders needs a return, select it and tell me what's wrong.`,
    );
    finishRun(s, { rounds: round, tools });
    return s;
  }

  const mine = ordersOf(p.customerId);
  const order = orderId ? mine.find((o) => o.order_id === orderId) : undefined;
  if (!order) {
    tools += 1;
    toolStep(s, "list_my_orders", {}, { evidence: { order_count: mine.length } }, { round: round++ });
    assistant(
      s,
      `Which order is this about? Select it in the order list, or tell me the order number. You have ${mine.length} orders on file: ${mine.map((o) => o.order_id).join(", ")}.`,
    );
    finishRun(s, { rounds: round, tools });
    return s;
  }

  const line = order.lines[0];
  p.conv.memo.orderId = order.order_id;
  const reason = it.defect ? "QUALITY_DAMAGE" : it.mind ? "CHANGE_OF_MIND" : it.fit ? "FIT_SIZE" : (p.conv.memo.reason ?? null);
  if (reason) p.conv.memo.reason = reason;
  if (it.direction !== "unknown") p.conv.memo.direction = it.direction;
  if (it.deadline) p.conv.memo.deadline = it.deadline;
  const direction = p.conv.memo.direction ?? "unknown";
  const deadline = p.conv.memo.deadline ?? null;

  tools += 1;
  toolStep(
    s,
    "get_order_context",
    { order_id: order.order_id },
    {
      latency: 120,
      evidence: { delivered_at: order.delivered_at, final_sale: line.final_sale, remaining_qty: line.purchased_qty - line.returned_or_reserved_qty },
      refs: [`ORDER#${order.order_id}/META`],
    },
    { round: round++ },
  );

  if (!reason) {
    assistant(s, `What's wrong with your ${line.product_name}? For example, the fit is off, it's damaged, or you changed your mind. One sentence is enough.`);
    finishRun(s, { rounds: round, tools });
    return s;
  }

  const condition = it.worn ? "worn" : reason === "QUALITY_DAMAGE" ? "damaged" : "unworn";
  const policy = evaluatePolicy(order, line, 1, reason, condition);
  const policyCheckId = newId("PCHK-", 8);
  tools += 1;
  toolStep(
    s,
    "evaluate_return_policy",
    { order_id: order.order_id, line_id: line.line_id, qty: 1, reason_category: reason, condition, size_direction: direction },
    {
      latency: 140,
      evidence: { decision: policy.decision, rule_ids: policy.rule_ids, policy_version: POLICY_VERSION, policy_check_id: policyCheckId },
      refs: [`DEMO-POLICY-CA@${POLICY_VERSION}`],
    },
    { round: round++ },
  );

  if (policy.decision === "ineligible") {
    s.emit(500, "decision", { summary: `Policy says ineligible (${policy.rule_ids.join(", ")}). I will not offer an exception.` });
    assistant(s, ineligibleText(policy.rule_ids[0], line));
    finishRun(s, { rounds: round, tools });
    return s;
  }

  if (policy.decision === "needs_review" || policy.decision === "needs_information") {
    const caseId = `DEMO-CASE-${hex(6)}`;
    s.emit(500, "decision", { summary: `Policy needs a person to review this (${policy.rule_ids.join(", ")}), so I will open a case instead of deciding.` });
    tools += 1;
    s.emit(600, "tool_call", {
      tool: "escalate_case",
      args: { reason_code: "POLICY_REVIEW", summary: `${line.product_name}: ${reason.toLowerCase().replace("_", " ")}. Rules: ${policy.rule_ids.join(", ")}.`, order_id: order.order_id, line_id: line.line_id },
      round: round,
    });
    s.emit(
      200,
      "case_created",
      { case_id: caseId, reason_code: "POLICY_REVIEW" },
      () => {
        state.cases.unshift({
          case_id: caseId,
          customer_id: p.customerId,
          reason_code: "POLICY_REVIEW",
          summary: `${line.product_name} (${order.order_id}): ${reason.toLowerCase().replace("_", " ")}. Policy result: ${policy.decision}. Rules: ${policy.rule_ids.join(", ")}.`,
          order_id: order.order_id,
          line_id: line.line_id,
          status: "open",
          created_at: iso(now()),
          run_id: p.runId,
        });
      },
    );
    s.emit(60, "tool_result", {
      tool: "escalate_case",
      ok: true,
      code: null,
      latency_ms: 260,
      evidence: { case_id: caseId },
      source_refs: [`CASE#${caseId}/META`],
      provenance: "synthetic",
      round: round++,
    });
    assistant(s, `I can't decide this one myself, so I've sent it to a person for review. Your case ID is ${caseId}. I can't promise an outcome, but they'll have the order details and what you told me.`);
    finishRun(s, { rounds: round, tools });
    return s;
  }

  // Eligible: look at fit evidence, find options, rank, and create a quote.
  quoteFlow(s, { p, order, line, reason, direction, deadline, policyCheckId, round, tools, policy });
  return s;
}

function ineligibleText(rule: string, line: MockLine): string {
  switch (rule) {
    case "final_sale.no_return":
      return `Your ${line.product_name} was bought as final sale, so under the demo policy it can't be returned for a change of mind or fit. If it's damaged or defective, tell me what happened and I'll send it to a person.`;
    case "window.expired":
      return `It has been more than 30 days since this order was delivered, so it's outside the demo return window. I can't make an exception.`;
    case "qty.exceeds_remaining":
      return `There is nothing left on this line to return: the quantity you asked for is more than what remains.`;
    case "condition.worn":
      return `Worn items can't be returned for a fit or change-of-mind reason under the demo policy. If it's damaged, tell me what happened.`;
    default:
      return `Under the demo policy this item isn't eligible (${rule}). I can't offer an exception.`;
  }
}

interface FlowCtx {
  p: AldoParams | RequoteParams;
  order: MockOrder;
  line: MockLine;
  reason: string;
  direction: string;
  deadline: string | null;
  policyCheckId: string;
  round: number;
  tools: number;
  policy: ReturnType<typeof evaluatePolicy>;
}

/** get_fit_insight (fit reasons), find_resolution_options, record_decision, create_quote, then the reply. */
function quoteFlow(s: Script, c: FlowCtx): void {
  let { round, tools } = c;
  const { p, order, line } = c;
  const fitKey = line.product_name;

  if (c.reason === "FIT_SIZE") {
    const fit = FITS[fitKey];
    tools += 1;
    if (fit) {
      const n = fit.too_small + fit.too_big;
      toolStep(
        s,
        "get_fit_insight",
        { product_name: fitKey },
        {
          latency: 70,
          evidence: { signal: fit.signal, too_small_share: Number((fit.too_small / n).toFixed(3)), n_fit: n, scope: "ALDO_CA" },
          provenance: "derived",
          refs: ["fit_insights.json"],
        },
        { round: round++ },
      );
    }
  }

  const cand = candidatesFor(p.customerId, line, c.policy, c.direction, c.deadline);
  tools += 1;
  const candidateSetId = newId("CAND-", 8);
  toolStep(
    s,
    "find_resolution_options",
    { policy_check_id: c.policyCheckId, ...(cand.replacementSize ? { replacement_size: cand.replacementSize } : {}), ...(c.deadline ? { deadline_date: c.deadline } : {}) },
    {
      latency: 210,
      evidence: { candidate_set_id: candidateSetId, option_types: [...new Set(cand.options.map((o) => o.type))].join(", "), dc_atp: cand.dcAtp, replacement_size: cand.replacementSize },
      refs: cand.replacementSku ? [`INV#${cand.replacementSku}`] : [`ORDER#${order.order_id}/META`],
    },
    { round: round++ },
  );

  const ranked = rankForQuote(cand.options);
  const exchanges = ranked.filter((o) => o.type.startsWith("exchange"));
  const top = exchanges[0];
  const dcMissing = cand.replacementSku && cand.dcAtp < 1;
  let why: string;
  if (top && c.deadline && top.meets_deadline) {
    why = `${dcMissing ? "The DC is out of the replacement size. " : ""}${top.location_name} pickup on ${weekdayText(top.eta_date)} is the only exchange that meets the deadline, so it ranks first. Refund options stay listed.`;
  } else if (top) {
    why = `An exchange keeps the customer in the right size, so I rank it first. Refund options stay listed.`;
  } else {
    why = `No exchange is possible (${cand.reasonNoExchange ?? "no stock"}), so I am offering the return options.`;
  }
  s.emit(500, "decision", { summary: why });

  const quoteId = newId("QUO-", 10);
  const optionIds = ranked.map((o) => o.option_id);
  tools += 1;
  s.emit(550, "tool_call", { tool: "create_quote", args: { candidate_set_id: candidateSetId, option_ids: optionIds }, round });
  const convRef = p.conv;
  const runId = p.runId;
  const customerId = p.customerId;
  const quotePayload: Record<string, unknown> = { quote_id: quoteId, option_ids: optionIds, expires_at: "" };
  s.emit(
    120,
    "quote_created",
    quotePayload,
    () => {
      const created = now();
      const expiresAt = created + QUOTE_TTL_MS;
      quotePayload.expires_at = iso(expiresAt);
      const previous = convRef.memo;
      if (previous.latestQuoteId) {
        const old = state.quotes.get(previous.latestQuoteId);
        if (old && old.stored_status === "open") old.stored_status = "superseded";
      }
      const options: QuoteOption[] = ranked.map((o) => ({ ...o, payload_hash: "" }));
      const quote: Quote = {
        quote_id: quoteId,
        customer_id: customerId,
        order_id: order.order_id,
        line_id: line.line_id,
        conv_id: convRef.conv_id,
        run_id: runId,
        policy_check_id: c.policyCheckId,
        candidate_set_id: candidateSetId,
        policy_version: POLICY_VERSION,
        currency: "CAD",
        created_at: iso(created),
        expiresAtMs: expiresAt,
        stored_status: "open",
        options,
        approval: null,
      };
      for (const o of quote.options) o.payload_hash = payloadHash(quoteId, o, POLICY_VERSION);
      state.quotes.set(quoteId, quote);
      previous.latestQuoteId = quoteId;
    },
  );
  s.emit(60, "tool_result", {
    tool: "create_quote",
    ok: true,
    code: null,
    latency_ms: 190,
    evidence: { quote_id: quoteId },
    source_refs: [`QUOTE#${quoteId}/META`],
    provenance: "synthetic",
    round: round++,
  });

  const fit = FITS[fitKey];
  const fitNote =
    c.reason === "FIT_SIZE" && fit && (fit.signal === "runs_small" || fit.signal === "runs_large")
      ? `${fitKey} ${fit.signal === "runs_small" ? "runs small" : "runs large"}: ${((Math.max(fit.too_small, fit.too_big) / (fit.too_small + fit.too_big)) * 100).toFixed(1)}% of fit-related returns say too ${fit.signal === "runs_small" ? "small" : "big"} (n=${fit.too_small + fit.too_big}). `
      : "";
  let text: string;
  if (top) {
    const later = exchanges.filter((o) => o.meets_deadline === false);
    text = `${fitNote}${dcMissing ? "The distribution centre is out of that size. " : ""}${top.location_name} can have size ${top.replacement_size} ready ${weekdayText(top.eta_date)}${top.meets_deadline ? ", which meets your deadline" : ""}.${later.length ? " Shipping options arrive later." : ""} You can still return for a refund if you prefer. Review the options and approve one; nothing happens until you do.`;
  } else {
    text = `${fitNote}I can't offer an exchange${cand.reasonNoExchange === "NO_SUCH_SIZE" ? " for this style" : ""}, but the return options are below. Review them and approve one; nothing happens until you do.`;
  }
  assistant(s, text);
  finishRun(s, { rounds: round, tools });
}

const FITS: Record<string, { signal: string; too_small: number; too_big: number }> = {
  "Stessy2.0": { signal: "runs_small", too_small: 465, too_big: 117 },
  Clementinne: { signal: "runs_large", too_small: 63, too_big: 354 },
  Dorenna: { signal: "runs_large", too_small: 139, too_big: 312 },
  Harriot: { signal: "runs_small", too_small: 92, too_big: 40 },
  Goer: { signal: "runs_small", too_small: 164, too_big: 69 },
  Lagoon: { signal: "runs_small", too_small: 123, too_big: 23 },
};

// ---- Re-quote after expiry ------------------------------------------------------------------

export interface RequoteParams {
  customerId: string;
  conv: Conv;
  runId: string;
  quote: Quote;
}

export function buildRequoteRun(p: RequoteParams): Script {
  const s = script();
  const order = orderById(p.quote.order_id) as MockOrder;
  const line = order.lines.find((l) => l.line_id === p.quote.line_id) as MockLine;
  s.emit(150, "run_started", { kind: "requote", conv_id: p.conv.conv_id, order_id: order.order_id, model_id: MODEL_ID, max_rounds: 8 });
  const reason = p.conv.memo.reason ?? "FIT_SIZE";
  const direction = p.conv.memo.direction ?? "unknown";
  const policy = evaluatePolicy(order, line, 1, reason, "unworn");
  const policyCheckId = newId("PCHK-", 8);
  toolStep(
    s,
    "evaluate_return_policy",
    { order_id: order.order_id, line_id: line.line_id, qty: 1, reason_category: reason, condition: "unworn", size_direction: direction },
    { latency: 130, evidence: { decision: policy.decision, rule_ids: policy.rule_ids, policy_version: POLICY_VERSION, policy_check_id: policyCheckId } },
    { round: 1 },
  );
  quoteFlow(s, { p, order, line, reason, direction, deadline: p.conv.memo.deadline ?? null, policyCheckId, round: 2, tools: 1, policy });
  return s;
}

// ---- Approval continuation --------------------------------------------------------------------

export interface ApprovalParams {
  customerId: string;
  conv: Conv;
  runId: string;
  quote: Quote;
  approval: Approval;
}

export function buildApprovalRun(p: ApprovalParams): Script {
  const s = script();
  const { quote, approval } = p;
  const opt = quote.options.find((o) => o.option_id === approval.option_id) as QuoteOption;
  const order = orderById(quote.order_id) as MockOrder;
  const line = order.lines.find((l) => l.line_id === quote.line_id) as MockLine;
  const isExchange = opt.type.startsWith("exchange");
  const txnId = isExchange ? `DEMO-RMA-${hex(6)}` : `DEMO-RET-${hex(6)}`;
  const updateId = isExchange ? `MU-${hex(6)}` : null;

  s.emit(0, "approval_received", { approval_id: approval.approval_id, quote_id: quote.quote_id, option_id: approval.option_id });
  s.emit(350, "run_started", { kind: "approval", conv_id: p.conv.conv_id, order_id: order.order_id, model_id: MODEL_ID, max_rounds: 8 });

  s.emit(700, "tool_call", { tool: "execute_approved_resolution", args: { quote_id: quote.quote_id, option_id: approval.option_id }, round: 1 });
  s.emit(
    380,
    "transaction_created",
    { transaction_id: txnId, state: "completed", option_type: opt.type, quote_id: quote.quote_id, replayed: false },
    () => {
      const txn: Txn = {
        transaction_id: txnId,
        customer_id: p.customerId,
        state: "completed",
        option_type: opt.type,
        order_id: order.order_id,
        line_id: line.line_id,
        qty: opt.qty,
        sku: opt.sku,
        location_id: opt.location_id,
        location_name: opt.location_name,
        replacement_size: opt.replacement_size,
        refund_cents: opt.refund_cents,
        fee_cents: opt.fee_cents,
        currency: opt.currency,
        eta_date: opt.eta_date,
        eta_text: opt.eta_text,
        next_step: nextStepText(opt),
        simulated: true,
        created_at: iso(now()),
        quote_id: quote.quote_id,
        update_id: updateId,
      };
      state.txns.set(txnId, txn);
      quote.stored_status = "executed";
      approval.consumed_txn_id = txnId;
      line.returned_or_reserved_qty += opt.qty;
      if (isExchange) takeStock(opt.sku, opt.location_id, opt.qty);
      pushA2a(p.customerId, "transaction_created", "aldo_agent", { transaction_id: txnId, state: "completed", option_type: opt.type, order_id: order.order_id, replayed: false });
    },
  );
  s.emit(80, "tool_result", {
    tool: "execute_approved_resolution",
    ok: true,
    code: null,
    latency_ms: 420,
    evidence: { transaction_id: txnId, replayed: false },
    source_refs: [`TXN#${txnId}/META`],
    provenance: "synthetic",
    round: 1,
  });

  // The write-back to the personal agent happens right after an exchange is created (provisional).
  if (isExchange && updateId) {
    s.emit(
      300,
      "decision",
      { summary: "Execution succeeded. Reading the saved record back before I tell the customer." },
      () => {
        const originalSize = line.size;
        const newSize = opt.replacement_size ?? originalSize;
        const reasonCode = Number(newSize) > Number(originalSize) ? "TOO_SMALL" : "TOO_BIG";
        addMemoryUpdate(p.customerId, {
          update_id: updateId,
          order_id: order.order_id,
          style_id: line.style,
          product_name: line.product_name,
          color: line.color,
          original_size: originalSize,
          new_size: newSize,
          reason: reasonCode,
          outcome: "exchange_created",
          provisional: true,
          source: "ALDO_CA",
          ts: iso(now()),
        });
        pushA2a(p.customerId, "memory_updated", "personal_agent", {
          update_id: updateId,
          style_id: line.style,
          product_name: line.product_name,
          original_size: originalSize,
          new_size: newSize,
          reason: reasonCode,
          outcome: "exchange_created",
          provisional: true,
        });
      },
    );
  } else {
    s.emit(300, "decision", { summary: "Execution succeeded. Reading the saved record back before I tell the customer." });
  }

  toolStep(
    s,
    "get_resolution_status",
    { transaction_id: txnId },
    { latency: 110, evidence: { state: "completed", location_id: opt.location_id }, refs: [`TXN#${txnId}/META`] },
    { round: 2, think: 450 },
  );
  assistant(
    s,
    `Done: simulated ${isExchange ? "exchange" : "return"} ${txnId} created. ${nextStepText(opt)} This is a prototype record only: no money moved and no parcel was sent.`,
  );
  finishRun(s, { rounds: 3, tools: 2 });
  return s;
}

