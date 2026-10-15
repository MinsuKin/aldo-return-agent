// Scripted stand-ins for ALDO bot's return workflow (fit exchanges and returns for the regression
// customers) and for the approval continuation. Each builder returns a timed list of run events (the
// same event types and payload shapes as contracts.md B.4 plus the pivot's A2A events). Side effects
// (quotes, orders, transactions, cases, memory) are applied when the event becomes visible, so
// the UI sees exactly what a real run would show, in the same order.

import type { MockLine, MockOrder } from "./data.ts";
import {
  DEMO_TODAY,
  MODEL_ID,
  POLICY_VERSION,
  QUOTE_TTL_MS,
  addDays,
  enqueueWriteback,
  candidatesFor,
  evaluatePolicy,
  finishRun,
  hex,
  iso,
  newId,
  newRetMemo,
  nextStepText,
  now,
  orderById,
  ordersOf,
  payloadHash,
  persona,
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
  const defect = /\b(broke|broken|defect\w*|damag\w*|crack\w*|tore|torn|split|fell apart|peeling)\b/.test(m);
  const mind = /(changed my mind|change of mind|don'?t want|do not want|no longer|not what i expected|return them for a refund|just want a refund)/.test(m);
  const small = /(too small|too tight|tight|half size up|size up|too narrow|pinch)/.test(m);
  const big = /(too big|too large|too loose|loose|half size down|size down)/.test(m);
  const fit = small || big || /(doesn'?t fit|don'?t fit|wrong size)/.test(m);
  const unworn = /(unworn|not worn|never worn|haven'?t worn|have not worn)/.test(m);
  const worn = !unworn && /\b(worn|wore|wearing)\b/.test(m);
  const deadline = /(saturday|tomorrow)/.test(m) ? addDays(DEMO_TODAY, 1) : /sunday/.test(m) ? addDays(DEMO_TODAY, 2) : null;
  return { mentioned, defect, mind, fit, direction: big && !small ? "too_big" : small ? "too_small" : "unknown", worn, deadline };
}

// ---- ALDO bot, return workflow (legacy fit-exchange engine) ------------------------------------

interface AldoParams {
  customerId: string;
  conv: Conv;
  runId: string;
  message: string;
  bodyOrderId: string | null;
}

/**
 * One turn of the return workflow for the regression customers. `into`: append to an existing script (a
 * hand-off run that continues in the return workflow). Events without an actor belong to the workflow.
 */
export function buildAldoRun(p: AldoParams, into?: Script): Script {
  const s = buildAldoTurn(p, into);
  for (const ev of s.events) if (!ev.actor) ev.actor = "aldo_agent";
  return s;
}

function buildAldoTurn(p: AldoParams, into?: Script): Script {
  const s = into ?? script();
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
      pushA2a(customerId, "quote_created", "aldo_agent", {
        quote_id: quoteId,
        primary_option_type: quote.options[0]?.type ?? null,
        option_types: quote.options.map((o) => o.type),
      });
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
    text = `${fitNote}${dcMissing ? "The distribution centre is out of that size. " : ""}${top.location_name} can have size ${top.replacement_size} ready ${weekdayText(top.eta_date)}${top.meets_deadline ? ", which meets your deadline" : ""}.${later.length ? " Shipping options arrive later." : ""} You can still return for a refund if you prefer. Review the options and confirm one; nothing happens until you do.`;
  } else {
    text = `${fitNote}I can't offer an exchange${cand.reasonNoExchange === "NO_SUCH_SIZE" ? " for this style" : ""}, but the return options are below. Review them and confirm one; nothing happens until you do.`;
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

const money = (cents: number): string => `CAD ${(cents / 100).toFixed(2)}`;

function doneText(opt: QuoteOption, who: { name: string; minutes: number | null }): string {
  const n = opt.lines?.length ?? 0;
  if (opt.type === "return_mail" && n > 0) {
    const what = n === 1 ? "the item" : `all ${n} items`;
    const shipping = opt.benefit
      ? `, and shipping was free with your ${opt.benefit.note.replace(/ perk.*/i, "")} perk (${opt.benefit.used} of ${opt.benefit.per_month} this month)`
      : opt.fee_cents > 0
        ? `, with the standard ${money(opt.fee_cents)} shipping fee`
        : "";
    return `Done. Your return label for ${what} is created${shipping}. Pack the parcel and drop it at any carrier point. Your refund of ${money(opt.refund_cents)} follows once ALDO receives it.`;
  }
  if (opt.type === "exchange_store_pickup") {
    const swap = opt.replacement_color ? `your ${opt.replacement_color.toLowerCase()} ${opt.replacement_size}` : `size ${opt.replacement_size}`;
    const near = who.minutes !== null ? `, ${who.minutes} min away` : "";
    return `Done. Your exchange is confirmed: ${swap} will be ready at ${opt.location_name}${near}. I've notified the store, so it's waiting for you. ${nextStepText(opt)}`;
  }
  if (opt.type === "return_in_store") return `Done. Your return is confirmed. ${nextStepText(opt)} I've notified the store.`;
  return `Done. Your ${opt.type.startsWith("exchange") ? "exchange" : "return"} is confirmed. ${nextStepText(opt)}`;
}

/** Continuation after the owner's confirm click: execute, read the record back, say what happened. */
export function buildApprovalRun(p: ApprovalParams): Script {
  const s = script();
  const A = "aldo_agent";
  const { quote, approval } = p;
  const opt = quote.options.find((o) => o.option_id === approval.option_id) as QuoteOption;
  const order = orderById(quote.order_id) as MockOrder;
  const lineIds = quote.line_ids ?? [quote.line_id];
  const lines = lineIds.map((id) => order.lines.find((l) => l.line_id === id) as MockLine);
  const line = lines[0];
  const isExchange = opt.type.startsWith("exchange");
  const txnId = isExchange ? `DEMO-RMA-${hex(6)}` : `DEMO-RET-${hex(6)}`;
  const updateId = isExchange ? `MU-${hex(6)}` : null;
  const who = persona(p.customerId);
  const minutes = opt.minutes_away ?? null;
  const store = opt.type === "exchange_store_pickup" || opt.type === "return_in_store";

  s.emit(0, "approval_received", { approval_id: approval.approval_id, quote_id: quote.quote_id, option_id: approval.option_id }, undefined, "system");
  s.emit(350, "run_started", { kind: "approval", conv_id: p.conv.conv_id, order_id: order.order_id, model_id: MODEL_ID, max_rounds: 8 }, undefined, A);

  s.emit(700, "tool_call", { tool: "execute_approved_resolution", args: { quote_id: quote.quote_id, option_id: approval.option_id }, round: 1 }, undefined, A);
  s.emit(
    380,
    "transaction_created",
    { transaction_id: txnId, state: "completed", option_type: opt.type, quote_id: quote.quote_id, replayed: false },
    () => {
      const created = iso(now());
      const swapColour = isExchange && opt.replacement_color ? opt.replacement_color : null;
      const notice = store
        ? {
            store_id: opt.location_id,
            store_name: opt.location_name,
            eta_minutes: minutes,
            summary:
              isExchange && swapColour
                ? `${who?.display_name ?? "A customer"} (${who?.tier_label ?? "member"}) is coming to swap ${line.color.toLowerCase()} ${line.size} for ${swapColour.toLowerCase()} ${opt.replacement_size}. Please hold ${swapColour.toLowerCase()} ${opt.replacement_size} at the desk.`
                : `${who?.display_name ?? "A customer"} (${who?.tier_label ?? "member"}) is bringing a pair back to the store.`,
            notified_at: created,
          }
        : null;
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
        replacement_color: swapColour,
        refund_cents: opt.refund_cents,
        fee_cents: opt.fee_cents,
        currency: opt.currency,
        eta_date: opt.eta_date,
        eta_text: opt.eta_text,
        next_step: nextStepText(opt),
        simulated: true,
        created_at: created,
        quote_id: quote.quote_id,
        update_id: updateId,
        ...(opt.lines ? { lines: opt.lines } : {}),
        ...(opt.benefit ? { benefit: opt.benefit } : {}),
        store_notice: notice,
      };
      state.txns.set(txnId, txn);
      quote.stored_status = "executed";
      approval.consumed_txn_id = txnId;
      for (const l of lines) l.returned_or_reserved_qty += opt.lines ? 1 : opt.qty;
      if (isExchange) takeStock(opt.sku, opt.location_id, opt.qty);
      pushA2a(p.customerId, "transaction_created", A, {
        transaction_id: txnId,
        state: "completed",
        option_type: opt.type,
        order_id: order.order_id,
        original_size: line.size,
        new_size: opt.replacement_size ?? line.size,
        location_id: opt.location_id,
        fee_cents: opt.fee_cents,
        replayed: false,
      });
      if (notice) {
        pushA2a(p.customerId, "store_notified", "aldo_store", {
          store_name: notice.store_name,
          eta_minutes: notice.eta_minutes,
          summary: notice.summary,
          transaction_id: txnId,
        });
      }
      if (opt.benefit) {
        const key = `${p.customerId}|${DEMO_TODAY.slice(0, 7)}`;
        const used = (state.ledger.get(key) ?? 0) + 1;
        state.ledger.set(key, used);
        pushA2a(p.customerId, "benefit_applied", A, {
          benefit: opt.benefit.type,
          label: opt.benefit.label,
          saved_cents: opt.benefit.saved_cents,
          used,
          per_month: opt.benefit.per_month,
          transaction_id: txnId,
        });
      }
    },
    A,
  );
  s.emit(
    80,
    "tool_result",
    {
      tool: "execute_approved_resolution",
      ok: true,
      code: null,
      latency_ms: 420,
      evidence: { transaction_id: txnId, replayed: false },
      source_refs: [`TXN#${txnId}/META`],
      provenance: "synthetic",
      round: 1,
    },
    undefined,
    A,
  );

  // The write-back to ALDO bot happens right after an exchange is created (provisional).
  s.emit(
    300,
    "decision",
    { summary: "Execution succeeded. Reading the saved record back before I tell the customer." },
    isExchange && updateId
      ? () => {
          const originalSize = line.size;
          const newSize = opt.replacement_size ?? originalSize;
          const colourSwap = !!opt.replacement_color && opt.replacement_color.toLowerCase() !== line.color.toLowerCase() && newSize === originalSize;
          const reasonCode = colourSwap ? "PREFERENCE" : Number(newSize) > Number(originalSize) ? "TOO_SMALL" : "TOO_BIG";
          // Queued, not delivered: the real API delivers it when ALDO bot's memory is next read.
          enqueueWriteback(p.customerId, {
            update_id: updateId,
            order_id: order.order_id,
            style_id: line.style,
            product_name: line.product_name,
            color: opt.replacement_color ?? line.color,
            original_size: originalSize,
            new_size: newSize,
            reason: reasonCode,
            outcome: "exchange_created",
            provisional: true,
            fit_feedback: colourSwap ? "size_ok" : null,
            source: "ALDO_CA",
            ts: iso(now()),
          });
        }
      : undefined,
    A,
  );

  toolStep(
    s,
    "get_resolution_status",
    { transaction_id: txnId },
    { latency: 110, evidence: { state: "completed", location_id: opt.location_id }, refs: [`TXN#${txnId}/META`] },
    {
      round: 2,
      think: 450,
      actor: A,
      onResult: () => {
        // Back to the purchase workflow once the return is done.
        if (p.conv.agent !== "personal") return;
        const prev = p.conv.ret;
        p.conv.mode = "bot";
        p.conv.returns_order_id = null;
        p.conv.ret = { ...newRetMemo(), lastReason: prev.reason ?? prev.lastReason, lastCondition: prev.condition ?? prev.lastCondition };
        pushA2a(p.customerId, "handback", A, { reason: "completed" });
      },
    },
  );
  assistant(s, doneText(opt, { name: who?.display_name ?? "", minutes }), A);
  finishRun(s, { rounds: 3, tools: 2, actor: "system" });
  return s;
}
