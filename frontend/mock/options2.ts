// Facts, policy, options and quotes for the colour exchange (John) and the bundle return (David).
// Mirrors addendum 11.1 D5 to D9 and 11.4: preference reasons allow exchanges, store options carry
// minutes_away, a bundle is one quote and one shipment, the Platinum perk waives one mail fee a month.

import { LOCATIONS, TIER_LIMIT_CENTS, daysBetween, skuFor, type MockLine, type MockLocation, type MockOrder, type MockPersona } from "./data.ts";
import { RET, say, type Counter } from "./common.ts";
import {
  DEMO_TODAY,
  POLICY_VERSION,
  QUOTE_TTL_MS,
  addDays,
  atp,
  condition,
  hex,
  iso,
  newId,
  newRetMemo,
  now,
  payloadHash,
  pushA2a,
  state,
  weekdayText,
  type Benefit,
  type BundleLine,
  type Conv,
  type Quote,
  type QuoteOption,
  type Script,
} from "./state.ts";

// ---- What the owner said -------------------------------------------------------------------------

export interface Facts {
  reason?: string;
  condition?: "unworn" | "worn";
  color?: string;
  channel?: "store" | "mail";
  lineIds: string[];
  direction?: "too_big" | "too_small";
  /** Asked to waive a fee or ignore the rules. */
  override: boolean;
  /** "3 of the 10 items": how many items the owner says they are returning. */
  count?: number;
}

const COLOR_RE = /\b(black|brown|tan|white|navy|grey|gray|green|blue|red)\b/;
const NUM_WORDS: Record<string, number> = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
export const OVERRIDE_RE = /(ignore|disregard|forget|bypass|override)\b.{0,40}\b(polic|rules?|instructions?)|make it free|waive|free of charge/;

/** Items named by position ("items 2, 5 and 8") or by description ("the tan boot"). */
export function pickLines(lower: string, order: MockOrder): string[] {
  if (order.lines.length <= 1) return [];
  const picked = new Set<string>();
  for (const m of lower.matchAll(/(?:items?|numbers?|no\.?|#)\s*((?:\d{1,2}\s*(?:,|and|&)?\s*)+)/g)) {
    for (const d of m[1].match(/\d{1,2}/g) ?? []) {
      const line = order.lines[Number(d) - 1];
      if (line) picked.add(line.line_id);
    }
  }
  if (/^\s*[\d\s,&]+(?:and\s*[\d\s,&]+)*\.?\s*$/.test(lower)) {
    for (const d of lower.match(/\d{1,2}/g) ?? []) {
      const line = order.lines[Number(d) - 1];
      if (line) picked.add(line.line_id);
    }
  }
  for (const l of order.lines) {
    const kindWord = (l.category ?? "").split(" ").at(-1) ?? "";
    const named = new RegExp(`\\b${l.product_name.toLowerCase()}\\b`).test(lower);
    const described = kindWord !== "" && new RegExp(`\\b${l.color.toLowerCase()}\\b`).test(lower) && new RegExp(`\\b${kindWord}s?\\b`).test(lower);
    if (named || described) picked.add(l.line_id);
  }
  return order.lines.filter((l) => picked.has(l.line_id)).map((l) => l.line_id);
}

export function understand(lower: string, order: MockOrder): Facts {
  const unworn = /(unworn|not worn|never worn|haven'?t worn|have not worn)/.test(lower);
  const worn = !unworn && /\b(worn|wore|wearing)\b/.test(lower);
  const big = /too (big|large|loose)|half size down|size down/.test(lower);
  const small = /too (small|tight|narrow)|half size up|size up|\btight\b/.test(lower);
  let reason: string | undefined;
  if (/\b(broke|broken|defect\w*|damag\w*|crack\w*|tore|torn|peeling|fell apart)\b/.test(lower)) reason = "QUALITY_DAMAGE";
  else if (big || small || /doesn'?t fit|don'?t fit|wrong size|half size/.test(lower)) reason = "FIT_SIZE";
  else if (/not what i expected|looks different|expected/.test(lower)) reason = "EXPECTATION";
  else if (/colou?r|outfit|don'?t like|do not like|dislike|changed my mind|don'?t want|no longer|not for me|doesn'?t suit/.test(lower)) reason = "CHANGE_OF_MIND";

  const colour = COLOR_RE.exec(lower)?.[1];
  const channel: Facts["channel"] = /in[- ]store|in person|at the store|drop (?:it |them )?off|walk in|come in/.test(lower)
    ? "store"
    : /\bmail\b|by post|courier|ship (?:it|them)|send (?:it|them) (?:back )?by|label/.test(lower)
      ? "mail"
      : undefined;
  const countMatch = /(\d{1,2}|one|two|three|four|five) of the (\d{1,2}|ten)/.exec(lower);
  const count = countMatch ? (NUM_WORDS[countMatch[1]] ?? Number(countMatch[1])) : undefined;

  return {
    reason,
    condition: unworn ? "unworn" : worn ? "worn" : undefined,
    color: order.lines.length === 1 && colour ? (colour === "gray" ? "GREY" : colour.toUpperCase()) : undefined,
    channel,
    lineIds: pickLines(lower, order),
    direction: big && !small ? "too_big" : small ? "too_small" : undefined,
    override: OVERRIDE_RE.test(lower),
    count,
  };
}

// ---- Policy (demo assumptions: addendum 11.1 D5 and D8) ---------------------------------------------

export const EXCHANGE_REASONS = ["FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND"];
const RANK = { eligible: 0, needs_review: 1, ineligible: 2 } as const;
export type Decision = keyof typeof RANK;

export interface PolicyResult {
  decision: Decision;
  rule_ids: string[];
  policy_version: string;
}

/** Per-line policy; the tier authority limit is checked on the total of the whole request. */
export function evaluateLine(
  order: MockOrder,
  line: MockLine,
  reason: string,
  cond: "unworn" | "worn",
  requestTotalCents: number,
  limitCents: number,
): PolicyResult {
  const rules: string[] = [];
  let decision: Decision = "eligible";
  const raise = (d: Decision) => {
    if (RANK[d] > RANK[decision]) decision = d;
  };
  const defect = reason === "QUALITY_DAMAGE";
  if (line.final_sale === true && !defect) {
    rules.push("final_sale.no_return");
    raise("ineligible");
  } else if (line.final_sale === true) {
    rules.push("final_sale.defect_review");
    raise("needs_review");
  }
  if (!order.delivered_at) {
    rules.push("window.missing_delivery_date");
    raise("needs_review");
  } else if (daysBetween(order.delivered_at, DEMO_TODAY) > 30) {
    rules.push("window.expired");
    raise("ineligible");
  } else rules.push("window.ok");
  if (cond === "worn" && !defect) {
    rules.push("condition.worn");
    raise("ineligible");
  }
  if (requestTotalCents > limitCents) {
    rules.push("authority.refund_limit");
    raise("needs_review");
  }
  if (decision === "eligible" && EXCHANGE_REASONS.includes(reason)) rules.push("reason.exchange_allowed");
  return { decision, rule_ids: rules, policy_version: POLICY_VERSION };
}

export const limitFor = (me: MockPersona): number => TIER_LIMIT_CENTS[me.tier];

// ---- Stores, perks ---------------------------------------------------------------------------------

export interface NearStore {
  loc: MockLocation;
  minutes: number;
}

/** Stores the customer can reach, nearest first (addendum 11.1 D6). */
export function reachableStores(me: MockPersona, maxMinutes = 150): NearStore[] {
  return LOCATIONS.filter((l) => l.type === "STORE")
    .map((loc) => ({ loc, minutes: me.travel_minutes[loc.location_id] ?? Number.POSITIVE_INFINITY }))
    .filter((r) => r.minutes <= maxMinutes)
    .sort((a, b) => a.minutes - b.minutes);
}

export const dcOf = (): MockLocation => LOCATIONS.find((l) => l.type === "DC") as MockLocation;

const monthKey = (customerId: string): string => `${customerId}|${DEMO_TODAY.slice(0, 7)}`;
export const perksUsed = (customerId: string): number => state.ledger.get(monthKey(customerId)) ?? 0;
/** Platinum: one free return shipping per calendar month (a demo assumption). */
export const perkAvailable = (me: MockPersona): boolean => me.tier === "platinum" && perksUsed(me.customer_id) < 1;

export function platinumBenefit(me: MockPersona): Benefit {
  const used = perksUsed(me.customer_id) + 1;
  return {
    type: "free_return_shipping",
    label: "Platinum free return shipping",
    saved_cents: 595,
    used,
    per_month: 1,
    note: `Platinum perk ${used} of 1 this month`,
  };
}

// ---- Options -----------------------------------------------------------------------------------------

type Draft = Omit<QuoteOption, "option_id">;

function draft(type: string, loc: MockLocation, etaDate: string, sku: string, extra: Partial<Draft> = {}): Draft {
  return {
    type,
    sku,
    replacement_size: null,
    location_id: loc.location_id,
    location_name: loc.name,
    ...(loc.address ? { location_address: loc.address } : {}),
    qty: 1,
    refund_cents: 0,
    fee_cents: 0,
    currency: "CAD",
    eta_date: etaDate,
    eta_text: weekdayText(etaDate),
    meets_deadline: null,
    conditions: condition(type),
    evidence: {},
    source_refs: [`DEMO-LOGISTICS-CA`, `DEMO-POLICY-CA@${POLICY_VERSION}`],
    provenance: "synthetic",
    ...extra,
  };
}

export interface ExchangeSpec {
  color: string;
  size: string;
}

export const tokenOf = (line: MockLine): string => line.style.replace("DEMO-STYLE-", "");

/** Same style, new colour or size: ready today in the nearest store with stock, or by mail from the DC. */
export function exchangeDrafts(me: MockPersona, line: MockLine, spec: ExchangeSpec): { store: Draft | null; mail: Draft | null; sku: string } {
  const sku = skuFor(tokenOf(line), spec.color, spec.size);
  const dc = dcOf();
  const near = reachableStores(me).find((r) => atp(sku, r.loc.location_id) >= 1);
  const swap = { replacement_size: spec.size, replacement_color: spec.color, original_size: line.size };
  const store = near
    ? draft("exchange_store_pickup", near.loc, DEMO_TODAY, sku, {
        ...swap,
        minutes_away: near.minutes,
        evidence: { atp: atp(sku, near.loc.location_id), inventory_version: 1, rate_id: "DEMO-RATE-XPICKUP" },
        source_refs: [`INV#${sku}/LOC#${near.loc.location_id}`, `DEMO-POLICY-CA@${POLICY_VERSION}`],
      })
    : null;
  const mail =
    atp(sku, dc.location_id) >= 1
      ? draft("exchange_ship_from_dc", dc, addDays(DEMO_TODAY, dc.ship_days), sku, {
          ...swap,
          evidence: { atp: atp(sku, dc.location_id), inventory_version: 1, rate_id: "DEMO-RATE-XSHIP" },
          source_refs: [`INV#${sku}/LOC#${dc.location_id}`, `DEMO-POLICY-CA@${POLICY_VERSION}`],
        })
      : null;
  return { store, mail, sku };
}

export const bundleLine = (l: MockLine): BundleLine => ({
  line_id: l.line_id,
  sku: l.sku,
  product_name: l.product_name,
  ...(l.category ? { category: l.category } : {}),
  color: l.color,
  size: l.size,
  qty: 1,
  refund_cents: l.unit_paid_cents,
  unit_paid_cents: l.unit_paid_cents,
});

/** Refund options for one or several lines of one order: in store when a store is within 120 minutes, by mail always. */
export function returnDrafts(me: MockPersona, lines: MockLine[]): { store: Draft | null; mail: Draft } {
  const gross = lines.reduce((sum, l) => sum + l.unit_paid_cents, 0);
  const dc = dcOf();
  const benefit = perkAvailable(me) ? platinumBenefit(me) : null;
  const fee = benefit ? 0 : 595;
  const items = lines.map(bundleLine);
  const near = reachableStores(me, 120)[0];
  const store = near
    ? draft("return_in_store", near.loc, DEMO_TODAY, lines[0].sku, {
        qty: lines.length,
        refund_cents: gross,
        minutes_away: near.minutes,
        lines: items,
        evidence: { rate_id: "DEMO-RATE-INSTORE" },
      })
    : null;
  const mail = draft("return_mail", dc, addDays(DEMO_TODAY, 10), lines[0].sku, {
    qty: lines.length,
    refund_cents: gross - fee,
    fee_cents: fee,
    lines: items,
    benefit,
    conditions: benefit
      ? ["Shipping is free with your Platinum perk", "Refund follows once ALDO receives the parcel"]
      : condition("return_mail"),
    evidence: { rate_id: "DEMO-RATE-MAIL", tier: me.tier, ...(benefit ? { free_return_shipping: "available" } : me.tier === "platinum" ? { free_return_shipping: "used this month" } : {}) },
  });
  return { store, mail };
}

/** Keep the order given, drop gaps, number the options OPT-1... */
export function numbered(drafts: (Draft | null | undefined)[]): QuoteOption[] {
  return drafts.filter((d): d is Draft => !!d).map((d, i) => ({ ...d, option_id: `OPT-${i + 1}` }));
}

// ---- Quotes and cases ---------------------------------------------------------------------------------

interface QuoteCtx {
  customerId: string;
  conv: Conv;
  runId: string;
}

/** create_quote, quote_created (server side effect: the quote exists), and the A2A mirror. */
export function emitQuote(
  s: Script,
  p: QuoteCtx,
  st: Counter,
  ctx: { order: MockOrder; lines: MockLine[]; options: QuoteOption[]; policyCheckId: string },
): string {
  const quoteId = newId("QUO-", 10);
  const candidateSetId = newId("CAND-", 8);
  const optionIds = ctx.options.map((o) => o.option_id);
  st.tools += 1;
  s.emit(550, "tool_call", { tool: "create_quote", args: { candidate_set_id: candidateSetId, option_ids: optionIds }, round: st.round }, undefined, RET);
  const payload: Record<string, unknown> = { quote_id: quoteId, option_ids: optionIds, expires_at: "" };
  s.emit(
    120,
    "quote_created",
    payload,
    () => {
      const created = now();
      const expiresAt = created + QUOTE_TTL_MS;
      payload.expires_at = iso(expiresAt);
      const previous = p.conv.memo.latestQuoteId ? state.quotes.get(p.conv.memo.latestQuoteId) : undefined;
      if (previous && previous.stored_status === "open") previous.stored_status = "superseded";
      const options: QuoteOption[] = ctx.options.map((o) => ({ ...o, payload_hash: "" }));
      const quote: Quote = {
        quote_id: quoteId,
        customer_id: p.customerId,
        order_id: ctx.order.order_id,
        line_id: ctx.lines[0].line_id,
        line_ids: ctx.lines.map((l) => l.line_id),
        conv_id: p.conv.conv_id,
        run_id: p.runId,
        policy_check_id: ctx.policyCheckId,
        candidate_set_id: candidateSetId,
        policy_version: POLICY_VERSION,
        currency: "CAD",
        created_at: iso(created),
        expiresAtMs: expiresAt,
        stored_status: "open",
        options,
        approval: null,
        v2: true,
      };
      for (const o of quote.options) o.payload_hash = payloadHash(quoteId, o, POLICY_VERSION);
      state.quotes.set(quoteId, quote);
      p.conv.memo.latestQuoteId = quoteId;
      p.conv.ret.latestQuoteId = quoteId;
      pushA2a(p.customerId, "quote_created", RET, {
        quote_id: quoteId,
        primary_option_type: options[0]?.type ?? null,
        option_types: options.map((o) => o.type),
      });
    },
    RET,
  );
  s.emit(
    60,
    "tool_result",
    { tool: "create_quote", ok: true, code: null, latency_ms: 190, evidence: { quote_id: quoteId }, source_refs: [`QUOTE#${quoteId}/META`], provenance: "synthetic", round: st.round++ },
    undefined,
    RET,
  );
  return quoteId;
}

/** The policy cannot decide: open a case for a person and say so (requirement 10). */
export function escalate(s: Script, p: QuoteCtx, st: Counter, order: MockOrder, lines: MockLine[], rules: string[]): void {
  const caseId = `DEMO-CASE-${hex(6)}`;
  s.emit(500, "decision", { summary: `Policy needs a person to review this (${rules.join(", ")}), so I will open a case instead of deciding.` }, undefined, RET);
  st.tools += 1;
  s.emit(
    600,
    "tool_call",
    { tool: "escalate_case", args: { reason_code: "POLICY_REVIEW", order_id: order.order_id, line_id: lines[0].line_id }, round: st.round },
    undefined,
    RET,
  );
  s.emit(
    200,
    "case_created",
    { case_id: caseId, reason_code: "POLICY_REVIEW" },
    () => {
      state.cases.unshift({
        case_id: caseId,
        customer_id: p.customerId,
        reason_code: "POLICY_REVIEW",
        summary: `${lines.length} item${lines.length === 1 ? "" : "s"} from ${order.order_id}. Policy result: needs review. Rules: ${rules.join(", ")}.`,
        order_id: order.order_id,
        line_id: lines[0].line_id,
        status: "open",
        created_at: iso(now()),
        run_id: p.runId,
      });
      if (p.conv.agent === "personal") {
        p.conv.mode = "bot";
        p.conv.returns_order_id = null;
        p.conv.ret = newRetMemo();
        pushA2a(p.customerId, "handback", RET, { reason: "escalated" });
      }
    },
    RET,
  );
  s.emit(
    60,
    "tool_result",
    { tool: "escalate_case", ok: true, code: null, latency_ms: 260, evidence: { case_id: caseId }, source_refs: [`CASE#${caseId}/META`], provenance: "synthetic", round: st.round++ },
    undefined,
    RET,
  );
  say(
    s,
    RET,
    `I can't decide this one myself, so I've sent it to a person for review. Your case ID is ${caseId}. I can't promise an outcome, but they'll have the order details and what you told me.`,
  );
}
