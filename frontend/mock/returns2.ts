// ALDO bot's return workflow, scripted for John (a colour exchange in store) and David (three of ten
// items returned by mail with the Platinum perk), plus the regression customers through the older engine.
// One owner message is one turn; what the owner already told the workflow lives in conv.ret.

import { MEMORY_SEEDS, MEN001, skuFor, sizeStr, type MockLine, type MockOrder, type MockPersona } from "./data.ts";
import { buildAldoRun } from "./agents.ts";
import { RET, cap, meOf, money, say, whoIs, type Counter, type Turn, type TurnParams } from "./common.ts";
import {
  EXCHANGE_REASONS,
  emitQuote,
  escalate,
  evaluateLine,
  exchangeDrafts,
  limitFor,
  numbered,
  perkAvailable,
  reachableStores,
  returnDrafts,
  understand,
  dcOf,
  type ExchangeSpec,
} from "./options2.ts";
import { MODEL_ID, POLICY_VERSION, atp, finishRun, newId, orderById, pushA2a, script, toolStep, weekdayText, type Script, type ToolOutcome } from "./state.ts";

const NUM_NAMES: Record<number, string> = { 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten" };

/** The return workflow reads the owner's saved preferences right after the hand-off (addendum 12.3). */
export function pushConsulted(customerId: string): void {
  const seed = MEMORY_SEEDS.find((m) => m.owner_id === customerId);
  const keptColours = (seed?.memory ?? []).filter((e) => e.outcome === "kept" && e.color);
  const fields = [keptColours.length ? "preferred_colors" : null, seed?.typical_size ? "typical_size" : null].filter((f): f is string => !!f);
  pushA2a(customerId, "memory_consulted", RET, { n_entries: seed?.memory.length ?? 0, fields });
}

/** Colours the owner kept elsewhere, most frequent first. */
function preferredColors(customerId: string): string[] {
  const seed = MEMORY_SEEDS.find((m) => m.owner_id === customerId);
  const counts = new Map<string, number>();
  for (const e of seed?.memory ?? []) if (e.outcome === "kept" && e.color) counts.set(e.color, (counts.get(e.color) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c);
}

function colourChoices(me: MockPersona, line: MockLine): { color: string; total: number; minutes: number | null }[] {
  const dc = dcOf();
  const stores = reachableStores(me);
  return MEN001.colors
    .filter((c) => c !== line.color)
    .map((color) => {
      const sku = skuFor(MEN001.token, color, line.size);
      const near = stores.find((r) => atp(sku, r.loc.location_id) >= 1);
      return { color, total: atp(sku, dc.location_id) + stores.reduce((n, r) => n + atp(sku, r.loc.location_id), 0), minutes: near ? near.minutes : null };
    })
    .filter((c) => c.total > 0);
}

export function itemList(order: MockOrder): string {
  const rows = order.lines.map((l, i) => `${i + 1}. ${l.product_name}${l.category ? ` ${l.category}` : ""}, ${l.color.toLowerCase()}, size ${l.size}, ${money(l.unit_paid_cents)}`);
  return `Here are the ${order.lines.length} items from order ${order.order_id}:\n${rows.join("\n")}`;
}

function ineligibleText(rule: string): string {
  switch (rule) {
    case "final_sale.no_return":
      return "That item was bought as final sale, so under the policy it can't be returned for this reason. If it's damaged or defective, tell me what happened and I'll send it to a person.";
    case "window.expired":
      return "It has been more than 30 days since this order was delivered, so it's outside the return window. I can't make an exception.";
    case "condition.worn":
      return "Worn items can't be returned for a fit or change-of-mind reason under the policy. If it's damaged, tell me what happened.";
    default:
      return `Under the policy this isn't eligible (${rule}). I can't offer an exception.`;
  }
}

/** A return owner message with no hand-off (the conversation is already in the return workflow). */
export function buildReturnsTurn(p: TurnParams): Turn {
  if (whoIs(p.customerId) === "legacy") {
    const bodyOrderId = p.conv.returns_order_id ?? p.conv.memo.orderId ?? null;
    return { script: buildAldoRun({ customerId: p.customerId, conv: p.conv, runId: p.runId, message: p.message, bodyOrderId }), kind: "customer", agent: "returns" };
  }
  const s = script();
  s.emit(150, "run_started", { kind: "returns", conv_id: p.conv.conv_id, order_id: p.conv.returns_order_id, model_id: MODEL_ID, max_rounds: 8 }, undefined, RET);
  v2Turn(s, p, { round: 1, tools: 0 }, false);
  return { script: s, kind: "customer", agent: "returns" };
}

/** Continue the same run in the return workflow (right after a hand-off). */
export function returnsInto(s: Script, p: TurnParams, st: Counter, consult: boolean): void {
  if (whoIs(p.customerId) === "legacy") {
    const bodyOrderId = p.conv.returns_order_id ?? p.conv.memo.orderId ?? null;
    buildAldoRun({ customerId: p.customerId, conv: p.conv, runId: p.runId, message: p.message, bodyOrderId }, s);
    return;
  }
  v2Turn(s, p, st, consult);
}

function v2Turn(s: Script, p: TurnParams, st: Counter, consult: boolean): void {
  const { customerId, conv } = p;
  const me = meOf(customerId);
  const ret = conv.ret;
  const order = orderById(ret.orderId ?? conv.returns_order_id ?? "");
  const lower = p.message.toLowerCase();

  let consultHook: (() => void) | undefined = consult ? () => pushConsulted(customerId) : undefined;
  const tool = (name: string, args: Record<string, unknown>, outcome: ToolOutcome) => {
    st.tools += 1;
    const onCall = consultHook;
    consultHook = undefined;
    toolStep(s, name, args, outcome, { round: st.round++, actor: RET, onCall });
  };
  const reply = (text: string) => {
    if (consultHook) {
      s.emit(80, "decision", { summary: "Using the owner's saved preferences." }, consultHook, RET);
      consultHook = undefined;
    }
    say(s, RET, text);
    finishRun(s, { rounds: st.round, tools: st.tools, actor: "system" });
  };

  if (!order) {
    reply("I can't find that order on your account. Which order would you like to return?");
    return;
  }
  if (!ret.contextRead) {
    ret.contextRead = true;
    tool("get_order_context", { order_id: order.order_id }, { latency: 120, evidence: { delivered_at: order.delivered_at, items: order.lines.length }, refs: [`ORDER#${order.order_id}/META`] });
  }

  const f = understand(lower, order);
  if (f.override) {
    ret.overrideAsked = true;
    s.emit(260, "decision", { summary: "A request to waive a fee or ignore the rules was not granted: perks and amounts come from the system, not from the chat." }, undefined, RET);
    const tail = ret.latestQuoteId ? "The options above are what I can offer." : "Tell me what happened and I'll show you what I can offer.";
    reply(
      me.tier === "platinum" && !perkAvailable(me)
        ? `I can't change that. Your free return shipping is once a month and it's already used, so the standard ${money(595)} shipping applies. Nothing in a message changes the rules. ${tail}`
        : `I can't waive fees or change the rules from a message. Amounts and perks come from the system, not from the chat. ${tail}`,
    );
    return;
  }

  if (f.reason) ret.reason = f.reason;
  if (f.condition) ret.condition = f.condition;
  if (f.direction) ret.direction = f.direction;
  if (f.color && f.color !== order.lines[0].color) ret.color = f.color;
  if (f.channel) ret.channel = f.channel;
  if (f.lineIds.length) ret.lineIds = f.lineIds;
  if (!(f.reason || f.condition || f.color || f.channel || f.lineIds.length || f.direction) && ret.latestQuoteId) {
    reply("The options are above. Tap the one you want to confirm, or tell me what you'd like to change.");
    return;
  }

  // Which items? (An order with several lines: the owner names them.)
  const multi = order.lines.length > 1;
  if (!multi && ret.lineIds.length === 0) ret.lineIds = [order.lines[0].line_id];
  if (multi && ret.lineIds.length === 0) {
    const which = f.count ? `Which ${NUM_NAMES[f.count] ?? f.count}` : "Which ones";
    reply(`${itemList(order)}\n${which} are you returning, and what's the reason?`);
    return;
  }
  const lines = ret.lineIds.map((id) => order.lines.find((l) => l.line_id === id)).filter((l): l is MockLine => !!l);

  const reason = ret.reason ?? ret.lastReason;
  if (!reason) {
    reply(`What's the reason for ${lines.length > 1 ? "returning them" : "the return"}?`);
    return;
  }
  const cond = ret.condition ?? ret.lastCondition;
  if (!cond) {
    reply("Are they unworn, in the original condition?");
    return;
  }
  const reusedAnswers = !ret.reason && !!ret.lastReason;

  // Policy, line by line; the tier limit is checked on the total of the request.
  const total = lines.reduce((sum, l) => sum + l.unit_paid_cents, 0);
  const policyKey = [reason, cond, lines.map((l) => l.line_id).join(",")].join("|");
  const checked = ret.policyKey === policyKey;
  const checks = lines.map((line, i) => ({
    line,
    id: checked ? (ret.policyIds?.[i] ?? newId("PCHK-", 8)) : newId("PCHK-", 8),
    res: evaluateLine(order, line, reason, cond, total, limitFor(me)),
  }));
  if (!checked) {
    for (const c of checks) {
      tool(
        "evaluate_return_policy",
        { order_id: order.order_id, line_id: c.line.line_id, qty: 1, reason_category: reason, condition: cond },
        { latency: 140, evidence: { decision: c.res.decision, rule_ids: c.res.rule_ids, policy_version: POLICY_VERSION, policy_check_id: c.id }, refs: [`DEMO-POLICY-CA@${POLICY_VERSION}`] },
      );
    }
  }
  const blocked = checks.find((c) => c.res.decision === "ineligible");
  if (blocked) {
    s.emit(500, "decision", { summary: `Policy says ineligible (${blocked.res.rule_ids.join(", ")}). I will not offer an exception.` }, undefined, RET);
    reply(ineligibleText(blocked.res.rule_ids.find((r) => !r.endsWith(".ok")) ?? blocked.res.rule_ids[0]));
    return;
  }
  const review = checks.find((c) => c.res.decision === "needs_review");
  if (review) {
    escalate(s, { customerId, conv, runId: p.runId }, st, order, lines, review.res.rule_ids);
    finishRun(s, { rounds: st.round, tools: st.tools, actor: "system" });
    return;
  }
  ret.policyKey = policyKey;
  ret.policyIds = checks.map((c) => c.id);

  // An exchange needs a new colour (preference reasons) or the next size (fit reasons); bundles are returns only.
  const line = lines[0];
  const single = lines.length === 1 && !multi;
  let spec: ExchangeSpec | null = null;
  let note = "";
  if (single && line.style === MEN001.style && EXCHANGE_REASONS.includes(reason)) {
    if (reason === "FIT_SIZE") {
      if (!ret.direction) {
        reply("Is the pair too big or too small? I'll look for the next size.");
        return;
      }
      spec = { color: line.color, size: sizeStr(Number(line.size) + (ret.direction === "too_big" ? -0.5 : 0.5)) };
    } else if (!ret.color) {
      const pref = preferredColors(customerId);
      const rank = (c: string) => (pref.indexOf(c) === -1 ? 99 : pref.indexOf(c));
      const choices = colourChoices(me, line).sort((a, b) => rank(a.color) - rank(b.color) || (a.minutes ?? 999) - (b.minutes ?? 999));
      tool(
        "find_resolution_options",
        { policy_check_id: checks[0].id },
        {
          latency: 210,
          evidence: { needs_choice: "replacement_color", colors: choices.map((c) => c.color).join(", ") || "none", replacement_size: line.size },
          refs: [`INV#${skuFor(MEN001.token, "BROWN", line.size)}`],
        },
      );
      if (choices.length === 0) {
        note = `There is no other colour in your size right now, so I'm showing the return options. `;
      } else {
        const top = choices[0];
        const rest = choices.slice(1);
        reply(
          pref.includes(top.color)
            ? `That's eligible, and an exchange is easier than a refund. ${cap(top.color)} is in stock in your size, which fits what you usually keep${rest.length ? `, and ${rest.map((c) => cap(c.color)).join(" and ")} ${rest.length > 1 ? "are" : "is"} available too` : ""}. Which colour would you like?`
            : `That's eligible, and an exchange is easier than a refund. In your size ${line.size} I have ${choices.map((c) => cap(c.color)).join(" and ")}. Which colour would you like?`,
        );
        return;
      }
    } else {
      spec = { color: ret.color, size: line.size };
    }
  }

  // In store or by mail?
  let channel = ret.channel;
  if (spec) {
    const ex = exchangeDrafts(me, line, spec);
    if (!ex.store && !ex.mail) {
      note = `${cap(spec.color)} ${spec.size} is out of stock everywhere right now, so I'm showing the return options. `;
      spec = null;
    } else if (!channel) {
      if (ex.store && ex.mail) {
        tool(
          "find_resolution_options",
          { policy_check_id: checks[0].id, replacement_color: spec.color, replacement_size: spec.size },
          { latency: 210, evidence: { option_types: "exchange_store_pickup, exchange_ship_from_dc", nearest_store: ex.store.location_name, minutes_away: ex.store.minutes_away }, refs: [`INV#${ex.sku}`] },
        );
        const what = spec.color === line.color ? `Size ${spec.size}` : `${cap(spec.color)} ${spec.size}`;
        reply(`${what} is in stock at ${ex.store.location_name}, ${ex.store.minutes_away} min away. Would you like to exchange in store, or by mail?`);
        return;
      }
      channel = ex.store ? "store" : "mail";
    }
  }
  if (!spec) {
    const rd = returnDrafts(me, lines);
    if (!channel) {
      if (rd.store) {
        tool("find_resolution_options", { policy_check_id: checks[0].id }, { latency: 210, evidence: { option_types: "return_in_store, return_mail", nearest_store: rd.store.location_name, minutes_away: rd.store.minutes_away }, refs: [`ORDER#${order.order_id}/META`] });
        reply(`${note}Your nearest ALDO store is ${rd.store.location_name}, ${rd.store.minutes_away} min away. Would you like to return in store, or by mail?`);
        return;
      }
      channel = "mail";
    }
  }

  // Build the options, rank them, and make the quote.
  const rd = returnDrafts(me, lines);
  let options: ReturnType<typeof numbered>;
  if (spec) {
    const ex = exchangeDrafts(me, line, spec);
    options = numbered([...(channel === "store" ? [ex.store, ex.mail] : [ex.mail, ex.store]), rd.store, rd.mail]);
  } else {
    options = numbered(channel === "store" && rd.store ? [rd.store, rd.mail] : [rd.mail, rd.store]);
  }
  const primary = options[0];
  tool(
    "find_resolution_options",
    { policy_check_ids: checks.map((c) => c.id), ...(spec ? { replacement_color: spec.color, replacement_size: spec.size } : {}) },
    {
      latency: 210,
      evidence: {
        option_types: [...new Set(options.map((o) => o.type))].join(", "),
        tier: me.tier,
        ...(primary.benefit ? { free_return_shipping: "available" } : {}),
        ...(lines.length > 1 ? { items: lines.length } : {}),
      },
      refs: [`ORDER#${order.order_id}/META`],
    },
  );
  const why = primary.type.startsWith("exchange")
    ? primary.type === "exchange_store_pickup"
      ? `The in-store exchange ranks first: it is free and ${primary.minutes_away} min away. Other exchange and refund options stay listed.`
      : "The exchange by mail ranks first, as the owner chose. Refund options stay listed."
    : primary.type === "return_in_store"
      ? `The in-store return ranks first: it is free and ${primary.minutes_away} min away. Return by mail stays listed.`
      : lines.length > 1 && primary.benefit
        ? "Return by mail ranks first because no store is within reach. The Platinum free return shipping applies once."
        : "Return by mail ranks first because no store is within reach. The standard shipping fee applies.";
  s.emit(500, "decision", { summary: why }, undefined, RET);
  emitQuote(s, { customerId, conv, runId: p.runId }, st, { order, lines, options, policyCheckId: checks[0].id });

  let text: string;
  if (primary.type === "exchange_store_pickup" && spec) {
    const swap = spec.color === line.color ? `size ${line.size} for size ${spec.size}` : `${cap(line.color)} ${line.size} for ${cap(spec.color)} ${spec.size}`;
    text = `${note}Here's your exchange: ${swap} at ${primary.location_name}. It's free and ready today. The other options below include a refund if you'd rather not exchange. Tap Confirm exchange when you're ready; nothing happens until you do.`;
  } else if (primary.type === "exchange_ship_from_dc" && spec) {
    text = `${note}Here's your exchange by mail: ${spec.color === line.color ? `size ${spec.size}` : `${cap(spec.color)} ${spec.size}`} ships from the distribution centre and should arrive ${weekdayText(primary.eta_date)}. It's free. The other options below include a refund. Tap Confirm exchange when you're ready; nothing happens until you do.`;
  } else if (primary.type === "return_mail" && lines.length >= 1) {
    const n = lines.length;
    const eligible = n === 1 ? "That item is eligible" : `All ${n} items are eligible`;
    const answers = reusedAnswers ? " I used the same reason as before." : "";
    const shipping = primary.benefit
      ? `As a Platinum member your return shipping is free this month (perk ${primary.benefit.used} of ${primary.benefit.per_month}), so you save ${money(primary.benefit.saved_cents)}.`
      : me.tier === "platinum"
        ? `Your free return shipping for this month is already used (1 of 1), so the standard ${money(primary.fee_cents)} shipping applies.`
        : `Standard shipping of ${money(primary.fee_cents)} is taken from the refund.`;
    const where = rd.store ? "" : ` There's no ALDO store within reach of you, so ${n === 1 ? "it goes" : "they go"} in one return by mail.`;
    text = `${note}${eligible}.${answers}${where} ${shipping} The refund total is ${money(primary.refund_cents)}. Confirm below when you're ready; nothing happens until you do.`;
  } else {
    text = `${note}Here are your return options. The first is the one I'd pick: you can bring the pair to ${primary.location_name}, ${primary.minutes_away} min away, and get ${money(primary.refund_cents)} back. Confirm one below; nothing happens until you do.`;
  }
  reply(text);
}
