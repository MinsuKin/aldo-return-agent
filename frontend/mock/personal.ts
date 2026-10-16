// ALDO bot (the personal agent), scripted.
//
// ALDO bot only PROPOSES a purchase: send_size_decision records a proposal on the size request and
// emits size_recommended. No order exists until the owner clicks Confirm (POST /purchases/{id}/confirm).

import { FIT, HERO, LOCATIONS, MEMORY_SEEDS, skuFor, sizeStr } from "./data.ts";
import {
  DEMO_TODAY,
  MODEL_ID,
  QUOTE_TTL_MS,
  addDays,
  atp,
  finishRun,
  iso,
  newId,
  now,
  persona,
  pushA2a,
  script,
  state,
  toolStep,
  weekdayText,
  type Conv,
  type OfferedOption,
  type Script,
} from "./state.ts";

export interface PersonalParams {
  customerId: string;
  conv: Conv;
  runId: string;
  message: string;
}

const ACTOR = "personal_agent";

const reply = (s: Script, text: string, delay = 500) => s.emit(delay, "assistant_message", { text }, undefined, ACTOR);

export function buildPersonalRun(p: PersonalParams): Script {
  const s = script();
  const lower = p.message.toLowerCase();
  const seed = MEMORY_SEEDS.find((m) => m.owner_id === p.customerId);
  const home = persona(p.customerId)?.home_region ?? "QC";
  s.emit(150, "run_started", { kind: "personal", conv_id: p.conv.conv_id, order_id: null, model_id: MODEL_ID, max_rounds: 6 }, undefined, ACTOR);

  const wantsStessy = /(stessy|heel)/.test(lower) && /(order|buy|get me|purchase|need)/.test(lower);
  if (!wantsStessy) {
    reply(s, "Tell me what to buy. In this demo I can shop the ALDO Stessy2.0 heels, for example: Order me the ALDO Stessy heels for the wedding Saturday.");
    finishRun(s, { rounds: 1, tools: 0, actor: ACTOR });
    return s;
  }

  const typical = Number(seed?.typical_size ?? "7.5");
  const deadline = /(saturday|tomorrow)/.test(lower) ? addDays(DEMO_TODAY, 1) : null;
  const entries = seed?.memory ?? [];
  const requestId = newId("SZR-", 6);
  let round = 1;
  let tools = 1;

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
    { round: round++, actor: ACTOR },
  );

  // What ALDO's gateway would offer right now: sizes with stock, store options in the owner's home region.
  const stores = LOCATIONS.filter((l) => l.type === "STORE" && l.region === home).sort((a, b) => (a.location_id < b.location_id ? -1 : 1));
  const dc = LOCATIONS.find((l) => l.type === "DC");
  let counter = 0;
  const offered: OfferedOption[] = [];
  for (const size of [6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0]) {
    const sku = skuFor(HERO.token, HERO.color, size);
    if (dc && atp(sku, dc.location_id) >= 1) {
      const eta = addDays(DEMO_TODAY, dc.ship_days);
      offered.push({
        option_id: `SO-${++counter}`,
        size: sizeStr(size),
        type: "ship_from_dc",
        sku,
        location_id: dc.location_id,
        location_name: dc.name,
        eta_date: eta,
        eta_text: weekdayText(eta),
        meets_deadline: deadline ? eta <= deadline : null,
        atp: atp(sku, dc.location_id),
      });
    }
    for (const st of stores) {
      const have = atp(sku, st.location_id);
      if (have < 1) continue;
      const eta = addDays(DEMO_TODAY, st.pickup_ready_days ?? 1);
      offered.push({
        option_id: `SO-${++counter}`,
        size: sizeStr(size),
        type: "store_pickup",
        sku,
        location_id: st.location_id,
        location_name: st.name,
        eta_date: eta,
        eta_text: weekdayText(eta),
        meets_deadline: deadline ? eta <= deadline : null,
        atp: have,
      });
    }
  }
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

  const counts = FIT[HERO.product];
  const n = counts.too_small + counts.too_big;
  const share = Number((counts.too_small / n).toFixed(3));
  const sharePct = `${(share * 100).toFixed(1)}%`;
  const fitSignal = { signal: "runs_small", too_small_share: share, n_fit: n, scope: "ALDO_CA", provenance: "derived" };
  const target = typical + 0.5;
  const bySize = [...sizesView].sort((a, b) => Math.abs(Number(a.size) - target) - Math.abs(Number(b.size) - target) || Number(b.size) - Number(a.size));
  const chosenSize = bySize[0]?.size;
  const choice = chosenSize
    ? offered
        .filter((o) => o.size === chosenSize)
        .sort((a, b) => {
          const am = a.meets_deadline === true ? 0 : 1;
          const bm = b.meets_deadline === true ? 0 : 1;
          if (am !== bm) return am - bm;
          if (a.eta_date !== b.eta_date) return a.eta_date < b.eta_date ? -1 : 1;
          return Number(b.location_id === "DEMO-STORE-MTL-01") - Number(a.location_id === "DEMO-STORE-MTL-01");
        })[0]
    : undefined;

  tools += 1;
  toolStep(
    s,
    "ask_aldo_size_assist",
    { product_query: "Stessy", color: "BLACK", ...(deadline ? { deadline_date: deadline } : {}) },
    {
      latency: 340,
      evidence: { size_request_id: requestId, signal: fitSignal.signal, too_small_share: share, n_fit: n, offered_sizes: sizesView.map((x) => x.size).join(", ") },
      provenance: "derived",
      refs: ["aldo-gateway/size-assist"],
    },
    {
      round: round++,
      actor: ACTOR,
      onCall: () => pushA2a(p.customerId, "size_assist_requested", "personal_agent", { product_query: "Stessy", color: "BLACK", deadline_date: deadline }),
      onResult: () => {
        const created = now();
        state.sizeRequests.set(requestId, {
          size_request_id: requestId,
          customer_id: p.customerId,
          style_id: HERO.style,
          product_name: HERO.product,
          color: HERO.color,
          price_cents: HERO.priceCents,
          currency: "CAD",
          deadline_date: deadline,
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
          style_id: HERO.style,
          product_name: HERO.product,
          ...fitSignal,
          ...(state.dataMode === "platform" ? { data_source: "aws-athena", query_execution_ids: ["mock-query-1", "mock-query-2"] } : {}),
        });
        pushA2a(p.customerId, "size_requested", "aldo_gateway", {
          size_request_id: requestId,
          sizes: sizesView,
          option_count: offered.length,
          question: `This style runs small (${sharePct} of fit-related returns were 'too small', n=${n}). Which size and option should I reserve for your owner?`,
          expires_at: iso(created + QUOTE_TTL_MS),
        });
      },
    },
  );

  if (!choice) {
    s.emit(500, "decision", { summary: "ALDO has no stock of this style in any size, so I will not propose a purchase." }, undefined, ACTOR);
    reply(s, "ALDO has no Stessy2.0 in stock in any size right now, so I have nothing to propose. I can check again later.");
    finishRun(s, { rounds: round, tools, actor: ACTOR });
    return s;
  }

  const publicWhy = "runs small per ALDO signal; owner size history supports a half size up";
  s.emit(
    550,
    "decision",
    {
      summary: `Half size up from the usual ${typical.toFixed(1)}: ${choice.type === "store_pickup" ? `${choice.location_name} pickup` : "DC shipping"} ${choice.meets_deadline ? "meets" : "misses"} the Saturday deadline. I will propose it, not order it.`,
    },
    undefined,
    ACTOR,
  );

  tools += 1;
  toolStep(
    s,
    "send_size_decision",
    { size_request_id: requestId, option_id: choice.option_id, chosen_size: choice.size, rationale_public: publicWhy, confidence: "high" },
    {
      latency: 380,
      evidence: { size_request_id: requestId, status: "proposed", awaiting: "owner_confirmation" },
      refs: [`SIZEREQ#${requestId}/META`],
    },
    {
      round: round++,
      actor: ACTOR,
      think: 700,
      onResult: () => {
        const sr = state.sizeRequests.get(requestId);
        if (!sr) return;
        sr.status = "proposed";
        sr.proposed_option_id = choice.option_id;
        sr.rationale_public = publicWhy;
        sr.confidence = "high";
        pushA2a(p.customerId, "size_recommended", "personal_agent", {
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
  reply(
    s,
    `Here's my proposal. Your usual size is ${typical.toFixed(1)}${bigOne ? `, and ${bigOne.retailer.replace(" (synthetic)", "")}'s ${bigOne.size} ran big for you` : ""}. But ALDO says Stessy2.0 runs small (${sharePct} of fit-related returns say too small, n=${n}), so I'd go with ${choice.size}. ${choice.type === "store_pickup" ? `Pickup at ${choice.location_name} on ${choice.eta_text}` : `Shipping from ${choice.location_name}, arriving ${choice.eta_text}`}${choice.meets_deadline ? " meets your wedding deadline" : " misses your wedding deadline"}. I haven't ordered anything yet. Please review the proposal below and confirm it.`,
    600,
  );
  finishRun(s, { rounds: round, tools, actor: ACTOR });
  return s;
}
