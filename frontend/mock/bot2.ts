// ALDO bot's purchase workflow, scripted (addendum 11.2, John act 1), and the hand-off to the return
// workflow. ALDO bot only PROPOSES a purchase: nothing is ordered until the owner clicks Confirm.

import { DAVID_ORDER_ID, FIT, INSTRUCTION_LIKE, LOCATIONS, MEMORY_SEEDS, MEN001, MEN001_SIZES, reviewsFor, skuFor, sizeStr, type MockPersona } from "./data.ts";
import { buildPersonalRun } from "./personal.ts";
import { BOT, meOf, say, whoIs, type Turn, type TurnParams } from "./common.ts";
import { OVERRIDE_RE, dcOf } from "./options2.ts";
import { returnsInto } from "./returns2.ts";
import {
  DEMO_TODAY,
  MODEL_ID,
  QUOTE_TTL_MS,
  addDays,
  atp,
  finishRun,
  iso,
  newId,
  newRetMemo,
  now,
  ordersOf,
  pushA2a,
  script,
  state,
  toolStep,
  weekdayText,
  type OfferedOption,
  type Script,
} from "./state.ts";

const RETURN_RE = /\b(return|returning|returned|exchange|swap|refund|send (?:it|them|these) back|money back)\b|too (?:small|big|tight|large|loose)|doesn.t fit|half size|\btight\b/;
const BUY_RE = /\b(buy|order|get me|purchase|need|want)\b/;
const MEN001_RE = /men ?001|black leather shoes?|toney/;
const STESSY_RE = /stessy|heel/;

const startRun = (s: Script, p: TurnParams, kind = "personal"): void => {
  s.emit(150, "run_started", { kind, conv_id: p.conv.conv_id, order_id: null, model_id: MODEL_ID, max_rounds: 6 }, undefined, BOT);
};

/** Messages ALDO bot answers without a tool: greetings, unknown requests. */
function plainTurn(p: TurnParams, text: string, decision?: string): Turn {
  const s = script();
  startRun(s, p);
  if (decision) s.emit(240, "decision", { summary: decision }, undefined, BOT);
  say(s, BOT, text);
  finishRun(s, { rounds: 1, tools: 0, actor: "system" });
  return { script: s, kind: "personal", agent: "personal" };
}

// ---- Hand-off to the return workflow ---------------------------------------------------------------

/** The order a return is about: David's collection, a mentioned order of the owner's, or the latest delivered agent order. */
function returnOrderFor(p: TurnParams): string | null {
  if (whoIs(p.customerId) === "david") return DAVID_ORDER_ID;
  const mine = ordersOf(p.customerId);
  const mentioned = /DEMO-[A-Z0-9-]+/i.exec(p.message)?.[0]?.toUpperCase();
  const own = mine.find((o) => o.order_id === mentioned);
  if (own) return own.order_id;
  return mine.find((o) => o.channel === "agent" && !!o.delivered_at)?.order_id ?? null;
}

function handoffTurn(p: TurnParams): Turn {
  const who = whoIs(p.customerId);
  const orderId = returnOrderFor(p);
  if (!orderId && who !== "legacy") {
    const waiting = ordersOf(p.customerId).some((o) => o.channel === "agent" && !o.delivered_at);
    return plainTurn(
      p,
      waiting
        ? "Your order hasn't been delivered yet, so there's nothing to return. Once it arrives, tell me and we'll sort it out."
        : "I don't see a delivered order to return yet. Once something has arrived, tell me and we'll sort it out.",
    );
  }

  // The return workflow works from this state as soon as the hand-off happens; the mode flips when the event lands.
  p.conv.returns_order_id = orderId;
  p.conv.ret = { ...newRetMemo(), orderId: orderId ?? undefined, lastReason: p.conv.ret.lastReason, lastCondition: p.conv.ret.lastCondition };

  const s = script();
  startRun(s, p);
  toolStep(
    s,
    "hand_off_to_returns",
    { order_id: orderId },
    { latency: 70, evidence: { order_id: orderId, mode: "returns" }, refs: orderId ? [`ORDER#${orderId}/META`] : [] },
    {
      round: 1,
      actor: BOT,
      onResult: () => {
        p.conv.mode = "returns";
        pushA2a(p.customerId, "handoff_to_returns", BOT, { order_id: orderId });
      },
    },
  );
  say(s, BOT, "Sure, let's sort out your return.");
  returnsInto(s, p, { round: 2, tools: 1 }, who !== "legacy");
  return { script: s, kind: "customer", agent: "personal" };
}

// ---- Buying the Toney -----------------------------------------------------------------------------

function offeredFor(me: MockPersona, color: string): OfferedOption[] {
  const dc = dcOf();
  const stores = LOCATIONS.filter((l) => l.type === "STORE" && l.region === me.home_region).sort((a, b) => (a.location_id < b.location_id ? -1 : 1));
  const out: OfferedOption[] = [];
  let n = 0;
  const add = (size: number, type: OfferedOption["type"], sku: string, loc: { location_id: string; name: string }, days: number) => {
    const eta = addDays(DEMO_TODAY, days);
    out.push({
      option_id: `SO-${++n}`,
      size: sizeStr(size),
      type,
      sku,
      location_id: loc.location_id,
      location_name: loc.name,
      eta_date: eta,
      eta_text: weekdayText(eta),
      meets_deadline: null,
      atp: atp(sku, loc.location_id),
    });
  };
  for (const size of MEN001_SIZES) {
    const sku = skuFor(MEN001.token, color, size);
    if (atp(sku, dc.location_id) >= 1) add(size, "ship_from_dc", sku, dc, dc.ship_days);
    const withStock = stores.filter((st) => atp(sku, st.location_id) >= 1);
    const shipper = [...withStock].sort((a, b) => a.ship_days - b.ship_days || (a.location_id < b.location_id ? -1 : 1))[0];
    if (shipper) add(size, "ship_from_store", sku, shipper, shipper.ship_days);
    for (const st of withStock) add(size, "store_pickup", sku, st, st.pickup_ready_days ?? 1);
  }
  return out;
}

function men001Purchase(p: TurnParams): Script {
  const s = script();
  const me = meOf(p.customerId);
  const seed = MEMORY_SEEDS.find((m) => m.owner_id === p.customerId);
  const entries = seed?.memory ?? [];
  const typical = Number(seed?.typical_size ?? "10");
  const color = "BLACK";
  const requestId = newId("SZR-", 6);
  let round = 1;
  let tools = 0;
  startRun(s, p);

  // ALDO bot's own memory first. Its details stay private to ALDO bot.
  tools += 1;
  toolStep(
    s,
    "recall_size_memory",
    {},
    {
      latency: 45,
      evidence: {
        typical_size: seed?.typical_size ?? "unknown",
        entries: entries.length,
        kept: entries.filter((e) => e.outcome === "kept").length,
        returned_too_big: entries.filter((e) => e.outcome === "returned_too_big").length,
      },
      refs: ["PA#memory"],
    },
    { round: round++, actor: BOT },
  );

  // What ALDO's data service offers right now.
  const offered = offeredFor(me, color);
  const sizesView = [...new Set(offered.map((o) => o.size))].map((size) => ({
    size,
    options: offered
      .filter((o) => o.size === size)
      .map((o) => ({
        option_id: o.option_id,
        type: o.type,
        location_id: o.location_id,
        location_name: o.location_name,
        eta_date: o.eta_date,
        eta_text: o.eta_text,
        meets_deadline: o.meets_deadline,
        atp: o.atp,
      })),
  }));
  const counts = FIT[MEN001.product];
  const n = counts.too_small + counts.too_big;
  const share = Number((counts.too_small / n).toFixed(3));
  const bigPct = `${((1 - share) * 100).toFixed(1)}%`;
  const fitSignal = { signal: "runs_large", too_small_share: share, n_fit: n, scope: "ALDO_CA", provenance: "derived" };

  tools += 1;
  toolStep(
    s,
    "ask_aldo_size_assist",
    { product_query: "Toney", color, channel_pref: "ship" },
    {
      latency: 340,
      evidence: { size_request_id: requestId, signal: fitSignal.signal, too_small_share: share, n_fit: n, offered_sizes: sizesView.map((x) => x.size).join(", ") },
      provenance: "derived",
      refs: ["aldo-gateway/size-assist"],
    },
    {
      round: round++,
      actor: BOT,
      onCall: () => pushA2a(p.customerId, "size_assist_requested", BOT, { product_query: "Toney", color, deadline_date: null, channel_pref: "ship" }),
      onResult: () => {
        const created = now();
        state.sizeRequests.set(requestId, {
          size_request_id: requestId,
          customer_id: p.customerId,
          style_id: MEN001.style,
          product_name: MEN001.product,
          color,
          price_cents: MEN001.priceCents,
          currency: "CAD",
          deadline_date: null,
          fit_signal: fitSignal,
          offered,
          status: "open",
          proposed_option_id: null,
          rationale_public: null,
          confidence: null,
          order_id: null,
          chosen_option_id: null,
          next_step: null,
          created_at: iso(created),
          expiresAtMs: created + QUOTE_TTL_MS,
        });
        pushA2a(p.customerId, "fit_signal_shared", "aldo_gateway", {
          size_request_id: requestId,
          style_id: MEN001.style,
          product_name: MEN001.product,
          ...fitSignal,
          // The AWS data platform door (docs/data-platform/DESIGN.md): only the curated source names itself.
          ...(state.dataMode === "platform" ? { data_source: "aws-athena", query_execution_ids: ["mock-query-1", "mock-query-2"] } : {}),
        });
        pushA2a(p.customerId, "size_requested", "aldo_gateway", {
          size_request_id: requestId,
          sizes: sizesView,
          option_count: offered.length,
          question: `This style runs large (${bigPct} of fit-related returns were 'too big', n=${n}). Which size and option should I reserve for your owner?`,
          expires_at: iso(created + QUOTE_TTL_MS),
        });
      },
    },
  );

  // Reviews are untrusted text: ALDO flags instruction-like ones, and ALDO bot leaves them out.
  const reviews = reviewsFor(MEN001.style);
  const flagged = reviews.filter((r) => INSTRUCTION_LIKE.test(`${r.title} ${r.text}`)).length;
  tools += 1;
  toolStep(
    s,
    "read_product_reviews",
    { style_id: MEN001.style, limit: 20 },
    {
      latency: 180,
      evidence: { n_reviews: reviews.length, flagged, excluded: flagged },
      refs: [`REVIEWS#${MEN001.style}`],
    },
    {
      round: round++,
      actor: BOT,
      onResult: () => pushA2a(p.customerId, "reviews_shared", "aldo_gateway", { style_id: MEN001.style, product_name: MEN001.product, n_reviews: reviews.length, n_flagged: flagged }),
    },
  );

  // One half size below the usual size: the style runs large and the reviews agree.
  const target = typical - 0.5;
  const sizes = [...new Set(offered.map((o) => Number(o.size)))].sort((a, b) => Math.abs(a - target) - Math.abs(b - target) || a - b);
  const bestSize = sizes[0];
  const rank = (t: string) => (t === "ship_from_dc" ? 0 : t === "ship_from_store" ? 1 : 2);
  const choice = offered
    .filter((o) => Number(o.size) === bestSize)
    .sort((a, b) => rank(a.type) - rank(b.type) || (a.eta_date < b.eta_date ? -1 : a.eta_date > b.eta_date ? 1 : 0))[0];

  if (!choice) {
    s.emit(500, "decision", { summary: "ALDO has no stock of this style in any size, so I will not propose a purchase." }, undefined, BOT);
    say(s, BOT, "ALDO has no stock of the Toney in black right now, so I have nothing to propose. I can check again later.");
    finishRun(s, { rounds: round, tools, actor: "system" });
    return s;
  }

  const publicWhy = "runs large per ALDO data and the reviews; a half size down";
  s.emit(
    550,
    "decision",
    { summary: `Usual size ${typical.toFixed(1)}; a boot elsewhere ran big; ALDO says runs large and the reviews agree. Half size down: ${choice.size}. One review had instructions in it; I ignored it. I will propose it, not order it.` },
    undefined,
    BOT,
  );
  tools += 1;
  toolStep(
    s,
    "send_size_decision",
    { size_request_id: requestId, option_id: choice.option_id, chosen_size: choice.size, rationale_public: publicWhy, confidence: "high" },
    { latency: 380, evidence: { size_request_id: requestId, status: "proposed", awaiting: "owner_confirmation" }, refs: [`SIZEREQ#${requestId}/META`] },
    {
      round: round++,
      actor: BOT,
      think: 700,
      onResult: () => {
        const sr = state.sizeRequests.get(requestId);
        if (!sr) return;
        sr.status = "proposed";
        sr.proposed_option_id = choice.option_id;
        sr.rationale_public = publicWhy;
        sr.confidence = "high";
        pushA2a(p.customerId, "size_recommended", BOT, {
          size_request_id: requestId,
          option_id: choice.option_id,
          chosen_size: choice.size,
          option_type: choice.type,
          location_name: choice.location_name,
          eta_date: choice.eta_date,
          rationale_public: publicWhy,
          confidence: "high",
        });
      },
    },
  );

  const bigOne = entries.find((e) => e.outcome === "returned_too_big");
  const how =
    choice.type === "store_pickup"
      ? `Pickup at ${choice.location_name} is ready ${weekdayText(choice.eta_date)}`
      : `It ships from ${choice.location_name} and should arrive ${weekdayText(choice.eta_date)}`;
  say(
    s,
    BOT,
    `Here's my proposal for the ${MEN001.product} in black. Your usual size is ${typical.toFixed(1)}${bigOne ? `, but the ${bigOne.size} ${bigOne.item} you returned elsewhere ran big` : ""}. ALDO's return data says this style runs large (${bigPct} of fit-related returns say too big, n=${n}), and the reviews agree, so I'd go half a size down to ${choice.size}.${flagged ? ` I skipped ${flagged === 1 ? "one review that tried" : `${flagged} reviews that tried`} to give me instructions.` : ""} ${how}. I haven't ordered anything yet. Please check the card below and confirm.`,
    600,
  );
  finishRun(s, { rounds: round, tools, actor: "system" });
  return s;
}

// ---- Routing of one owner message while ALDO bot is in its purchase workflow ---------------------------

export function buildBotTurn(p: TurnParams): Turn {
  const lower = p.message.toLowerCase();
  const who = whoIs(p.customerId);
  const buy = BUY_RE.test(lower);

  if (who !== "legacy" && OVERRIDE_RE.test(lower)) {
    return plainTurn(
      p,
      "I can't change prices, fees or the rules from a message. I can buy shoes for you, or start an exchange or a return.",
      "A request to waive a fee or ignore the rules is not something I can grant; amounts and perks come from the system.",
    );
  }
  if (RETURN_RE.test(lower)) return handoffTurn(p);
  if (buy && MEN001_RE.test(lower)) return { script: men001Purchase(p), kind: "personal", agent: "personal" };
  if (buy && STESSY_RE.test(lower)) return { script: buildPersonalRun(p), kind: "personal", agent: "personal" };

  return plainTurn(
    p,
    who === "legacy"
      ? "Tell me what to buy. In this demo I can shop the ALDO Stessy2.0 heels, for example: Order me the ALDO Stessy heels for the wedding Saturday."
      : who === "david"
        ? "I can help with an exchange or a return, or buy shoes for you. What would you like?"
        : "I can buy shoes for you, like the Toney, or help with an exchange or a return. What would you like?",
  );
}
