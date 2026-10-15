import type { Provenance, RunEvent } from "../api/types";
import type { IconName } from "../components/Icon";
import { fitHeadline } from "./fit";
import { colorWord, formatCents, friendlyDate } from "./format";
import { OPTION_TYPE_LABEL, PURCHASE_OPTION_LABEL, credibleProvenance, provenanceKinds, transactionKind } from "./normalize";
import { isTerminal, runEventActor, runMetrics, toolSteps, type Chip, type RunAgent, type RunState, type ToolStep } from "./runs";
import { humanize, isObj, num, pick, str, type Obj } from "./util";

// The right-hand A2A trace: the public agent-to-agent stream, a few public milestones of the return
// workflow, and one collapsed group of tool calls per run. Owner-private material (ALDO bot's memory
// and notes, review text) never reaches this module's output.

export type Tone = "default" | "success" | "attention";
export interface Flag {
  label: string;
  tone: "amber" | "green" | "neutral";
}

export interface EventItem {
  kind: "event";
  key: string;
  tsMs: number;
  actor: string;
  type: string;
  icon: IconName;
  title: string;
  detail?: string;
  chips: Chip[];
  provenance: Provenance[];
  flags: Flag[];
  tone: Tone;
}

export interface RunItem {
  kind: "run";
  key: string;
  tsMs: number;
  actor: string;
  runId: string;
  agent: RunAgent;
  steps: ToolStep[];
  active: boolean;
  status: string;
  currentTool?: string;
  latencyMs?: number;
}

export type TraceItem = EventItem | RunItem;

/** Who sent an event when the row carries no `actor` (older servers). */
export const DEFAULT_ACTOR: Record<string, string> = {
  size_assist_requested: "personal_agent",
  fit_signal_shared: "aldo_gateway",
  size_requested: "aldo_gateway",
  reviews_shared: "aldo_gateway",
  size_recommended: "personal_agent",
  size_confirmed: "aldo_gateway",
  memory_updated: "personal_agent",
  handoff_to_returns: "personal_agent",
  memory_consulted: "aldo_agent",
  handback: "aldo_agent",
  quote_created: "aldo_agent",
  transaction_created: "aldo_agent",
  benefit_applied: "aldo_agent",
  store_notified: "aldo_store",
  delivered: "system",
};

export function a2aActor(ev: RunEvent): string {
  const fromPayload = isObj(ev.payload) ? str(ev.payload.actor) : undefined;
  return ev.actor ?? fromPayload ?? DEFAULT_ACTOR[ev.type] ?? "aldo_agent";
}

type Described = Pick<EventItem, "icon" | "title" | "detail" | "chips" | "provenance" | "flags" | "tone">;

const plain = (icon: IconName, title: string, detail?: string, extra: Partial<Described> = {}): Described => ({
  icon,
  title,
  detail,
  chips: [],
  provenance: [],
  flags: [],
  tone: "default",
  ...extra,
});

const OPTION_LABEL = (t: string): string => OPTION_TYPE_LABEL[t] ?? PURCHASE_OPTION_LABEL[t] ?? humanize(t);

function sizesOffered(p: Obj): string[] {
  const raw = Array.isArray(p.sizes) ? p.sizes : Array.isArray(p.offered_sizes) ? p.offered_sizes : [];
  return raw.map((s) => (isObj(s) ? str(s.size) : str(s))).filter((s): s is string => !!s);
}

/** Text-like keys never become chips (review text, free text). */
const TEXTY_KEY = /text|comment|body|title|message|review/i;

function scalarChips(p: Obj): Chip[] {
  return Object.entries(p)
    .filter(([k, v]) => !TEXTY_KEY.test(k) && (typeof v === "string" || typeof v === "number" || typeof v === "boolean"))
    .slice(0, 6)
    .map(([k, v]) => ({ label: humanize(k), value: String(v) }));
}

/** One-line public description of an A2A event. Tolerant of missing fields. */
export function describeEvent(type: string, p: Obj): Described {
  const product = str(pick(p, ["product_name", "product_query", "style"]));
  const color = str(p.color);

  switch (type) {
    case "size_assist_requested": {
      const deadline = str(p.deadline_date);
      return plain(
        "question",
        `Size help requested${product ? ` for ${product}` : ""}${color ? ` (${colorWord(color)})` : ""}`,
        deadline ? `Deadline ${friendlyDate(deadline)}` : undefined,
      );
    }
    case "fit_signal_shared": {
      const sig = isObj(p.fit_signal) ? p.fit_signal : p;
      const signal = str(sig.signal);
      return plain("signal", `Fit signal shared: ${signal ? signal.replace(/_/g, " ") : "signal"}`, fitHeadline(sig)?.replace(/^[^:]+: /, ""), {
        provenance: credibleProvenance(provenanceKinds(sig.provenance, p.provenance, "derived")),
      });
    }
    case "size_requested": {
      const offered = sizesOffered(p);
      const shown = offered.length > 8 ? `${offered.slice(0, 8).join(", ")}...` : offered.join(", ");
      return plain("question", `Asked which size and option to reserve${shown ? ` (in stock: ${shown})` : ""}`, str(p.question));
    }
    case "reviews_shared": {
      const n = num(pick(p, ["n_reviews", "count", "total", "reviews_count"]));
      const flagged = num(pick(p, ["n_flagged", "flagged", "flagged_count"])) ?? 0;
      return plain(
        "reviews",
        `Product reviews shared${n !== undefined ? `: ${n}` : ""}`,
        flagged > 0 ? `${flagged} flagged as instruction-like text and excluded` : "None flagged",
        flagged > 0 ? { flags: [{ label: `${flagged} flagged`, tone: "amber" }] } : {},
      );
    }
    case "size_recommended": {
      const size = str(pick(p, ["chosen_size", "size"]));
      const conf = str(p.confidence);
      const optionType = str(p.option_type);
      const where = str(p.location_name);
      const how = optionType ? (PURCHASE_OPTION_LABEL[optionType] ?? humanize(optionType)).toLowerCase() : undefined;
      const why = str(pick(p, ["rationale_public", "rationale"]));
      return plain(
        "decision",
        `Proposed size ${size ?? "?"}${how ? `, ${how}` : ""}${where ? ` at ${where}` : ""}`,
        `${why ?? ""}${why && conf ? " " : ""}${conf ? `(${conf} confidence)` : ""}`.trim() || undefined,
      );
    }
    case "size_confirmed": {
      const order = str(pick(p, ["order_id", "id"]));
      const line = isObj(p.line) ? p.line : undefined;
      const size = str(pick(p, ["chosen_size", "size"])) ?? (line ? str(line.size) : undefined);
      const ful = isObj(p.fulfillment) ? p.fulfillment : p;
      const where = str(pick(ful, ["location_name"]));
      const eta = str(pick(ful, ["eta_date"]));
      const byOwner = str(p.approved_by) === "owner_click";
      const detail = [byOwner ? "Confirmed by the owner" : undefined, where, eta ? friendlyDate(eta) : undefined].filter(Boolean).join(" · ");
      return plain("check", `Order placed${order ? `: ${order}` : ""}${size ? `, size ${size}` : ""}`, detail || undefined, { tone: "success" });
    }
    case "delivered": {
      const size = str(pick(p, ["size", "chosen_size"]));
      const order = str(p.order_id);
      return plain("delivered", `Delivered${product ? `: ${product}` : ""}${size ? `, size ${size}` : ""}`, order ? `Order ${order}` : undefined, {
        tone: "success",
      });
    }
    case "handoff_to_returns": {
      const order = str(p.order_id);
      return plain("handoff", "Switching to the return workflow", order ? `Order ${order}` : undefined);
    }
    case "memory_consulted": {
      const n = num(pick(p, ["n_entries", "entries", "count"]));
      const fields = Array.isArray(p.fields) ? p.fields.map((f) => str(f)).filter((f): f is string => !!f) : [];
      const readable = fields.map((f) => humanize(f).toLowerCase().replace(/color/g, "colour")).join(", ");
      const detail = [n !== undefined ? `${n} saved ${n === 1 ? "entry" : "entries"}` : undefined, readable || undefined].filter(Boolean).join(": ");
      return plain("preferences", "Using your preferences", detail || undefined);
    }
    case "handback": {
      const reason = str(pick(p, ["reason", "outcome"]));
      const detail = reason === "completed" ? "Return finished" : reason === "escalated" ? "Passed to a person for review" : undefined;
      return plain("handoff", "Back to the purchase workflow", detail);
    }
    case "quote_created": {
      const primary = str(pick(p, ["primary_option_type", "primary"]));
      const types = Array.isArray(p.option_types) ? p.option_types.map((t) => str(t)).filter((t): t is string => !!t) : [];
      const n = types.length || (Array.isArray(p.option_ids) ? p.option_ids.length : undefined);
      const refunds = types.some((t) => t.startsWith("return"));
      const id = str(pick(p, ["quote_id", "id"]));
      const detail = primary ? `${OPTION_LABEL(primary)} first${refunds ? ", refund options stay available" : ""}` : undefined;
      return plain("quote", `Options ready${n ? ` (${n})` : ""}`, detail, { chips: id ? [{ label: "Quote", value: id }] : [] });
    }
    case "transaction_created": {
      const id = str(pick(p, ["transaction_id", "id"])) ?? "";
      const optionType = str(p.option_type);
      const kind = transactionKind(id, optionType);
      const title = optionType === "return_mail" ? "Return label created" : kind === "exchange" ? "Exchange confirmed" : "Return confirmed";
      const from = str(p.original_size);
      const to = str(p.new_size);
      const fee = num(p.fee_cents);
      const detail = [
        optionType ? OPTION_LABEL(optionType) : undefined,
        from && to ? (from === to ? `size ${to}` : `size ${from} to ${to}`) : undefined,
        fee !== undefined ? (fee === 0 ? "no fee" : `fee ${formatCents(fee)}`) : undefined,
      ]
        .filter(Boolean)
        .join(" · ");
      return plain("transaction", `${title}${id ? `: ${id}` : ""}`, detail || undefined, { tone: "success" });
    }
    case "store_notified": {
      const store = str(pick(p, ["store_name", "store"]));
      const minutes = num(pick(p, ["eta_minutes", "minutes_away"]));
      return plain("store", `Store notified${store ? `: ${store}` : ""}`, str(p.summary), {
        chips: minutes !== undefined ? [{ label: "Customer", value: `${minutes} min away` }] : [],
      });
    }
    case "benefit_applied": {
      const label = str(pick(p, ["label", "benefit"])) ?? "free return shipping";
      const saved = num(pick(p, ["saved_cents", "saved"]));
      const used = num(p.used);
      const perMonth = num(p.per_month);
      const detail = [
        saved !== undefined ? `Saved ${formatCents(saved)}` : undefined,
        used !== undefined && perMonth !== undefined ? `${used} of ${perMonth} this month` : undefined,
      ]
        .filter(Boolean)
        .join(" · ");
      return plain("benefit", `Benefit applied: ${humanize(label).toLowerCase()}`, detail || undefined, { tone: "success" });
    }
    case "memory_updated": {
      const from = str(p.original_size);
      const to = str(p.new_size);
      const provisional = p.provisional === true;
      const feedback = str(p.fit_feedback);
      const outcome = str(p.outcome);
      const title = feedback === "size_ok" && to ? `Memory updated: size ${to} fits` : `Memory updated${from && to ? `: ${from} to ${to}` : ""}`;
      const detail = [product, color ? colorWord(color) : undefined, feedback === "size_ok" ? "kept when exchanging colour" : outcome ? humanize(outcome).toLowerCase() : undefined]
        .filter(Boolean)
        .join(", ");
      return plain("memory", title, detail || undefined, {
        flags: [{ label: provisional ? "provisional" : "confirmed", tone: provisional ? "amber" : "green" }],
      });
    }
    default:
      return plain("message", humanize(type), undefined, { chips: scalarChips(p) });
  }
}

// ---- Public milestones of the return workflow ------------------------------------------------------

function milestonesOf(run: RunState, mirrored: { quotes: Set<string>; txns: Set<string> }): EventItem[] {
  const out: EventItem[] = [];
  const lastArgs = new Map<string, Obj>();
  for (const ev of [...run.events].sort((a, b) => a.seq - b.seq)) {
    const p: Obj = isObj(ev.payload) ? ev.payload : {};
    const actor = runEventActor(ev, run);
    if (actor === "personal_agent") continue; // ALDO bot's own notes stay private
    const base = { kind: "event" as const, key: `${run.runId}-m-${ev.seq}`, tsMs: parseTs(ev.ts), actor, type: ev.type };

    if (ev.type === "tool_call") {
      const tool = str(pick(p, ["tool", "tool_name", "name"]));
      const args = pick(p, ["args", "arguments", "input"]);
      if (tool && isObj(args)) lastArgs.set(tool, args);
    } else if (ev.type === "tool_result" && str(p.tool) === "evaluate_return_policy" && p.ok !== false) {
      const evidence: Obj = isObj(p.evidence) ? p.evidence : {};
      const decision = str(evidence.decision);
      const rules = Array.isArray(evidence.rule_ids) ? evidence.rule_ids.map((r) => String(r)) : [];
      const version = str(evidence.policy_version);
      const lineId = str(lastArgs.get("evaluate_return_policy")?.line_id);
      const detail = [lineId ? `Item ${lineId}` : undefined, rules.length ? `Rules: ${rules.join(", ")}` : undefined].filter(Boolean).join(" · ");
      out.push({
        ...base,
        ...plain("policy", `Policy decision: ${decision ? decision.replace(/_/g, " ") : "checked"}`, detail || undefined, {
          chips: version ? [{ label: "Policy", value: version }] : [],
          tone: decision === "eligible" ? "success" : "attention",
        }),
      });
    } else if (ev.type === "decision") {
      const text = str(pick(p, ["summary", "text", "rationale"]));
      if (text) out.push({ ...base, ...plain("decision", "Decision", text) });
    } else if (ev.type === "approval_received") {
      out.push({ ...base, ...plain("approval", "Owner confirmed the chosen option", str(p.option_id) ? `Option ${str(p.option_id)}` : undefined, { tone: "success" }) });
    } else if (ev.type === "case_created") {
      const id = str(pick(p, ["case_id", "id"]));
      out.push({ ...base, ...plain("case", "Sent to a person for review", id ? `Case ${id}` : undefined, { tone: "attention" }) });
    } else if (ev.type === "quote_created") {
      const id = str(pick(p, ["quote_id", "id"])) ?? "";
      if (!mirrored.quotes.has(id)) out.push({ ...base, ...describeEvent("quote_created", p) });
    } else if (ev.type === "transaction_created") {
      const id = str(pick(p, ["transaction_id", "id"])) ?? "";
      if (!mirrored.txns.has(id)) out.push({ ...base, ...describeEvent("transaction_created", p) });
    }
  }
  return out;
}

function parseTs(ts: string | undefined): number {
  if (!ts) return Number.NaN;
  const t = Date.parse(ts);
  return Number.isNaN(t) ? Number.NaN : t;
}

/** Merge the A2A stream, public milestones and one group per run into a single chronological list. */
export function buildTimeline(a2a: RunEvent[], runs: RunState[]): TraceItem[] {
  const items: TraceItem[] = [];
  const mirrored = { quotes: new Set<string>(), txns: new Set<string>() };

  for (const run of runs) {
    const first = [...run.events].sort((a, b) => a.seq - b.seq)[0];
    const metrics = runMetrics(run);
    const active = !isTerminal(run.status);
    // One group per stretch of tool calls by the same workflow: a hand-off inside one run shows as two groups.
    const segments: ToolStep[][] = [];
    for (const step of toolSteps(run)) {
      const current = segments.at(-1);
      if (current && current[0].actor === step.actor) current.push(step);
      else segments.push([step]);
    }
    if (segments.length === 0) segments.push([]);
    segments.forEach((steps, i) => {
      const lastSegment = i === segments.length - 1;
      if (steps.length === 0 && !(active && lastSegment)) return; // a finished run without tool calls adds no row
      const pending = [...steps].reverse().find((s) => s.pending);
      items.push({
        kind: "run",
        key: `run-${run.runId}-${i}`,
        tsMs: steps.length ? steps[0].tsMs : first ? parseTs(first.ts) : Number.NaN,
        actor: steps[0]?.actor ?? (run.agent === "personal" ? "personal_agent" : "aldo_agent"),
        runId: run.runId,
        agent: run.agent,
        steps,
        active: active && lastSegment,
        status: run.status,
        currentTool: pending?.label,
        latencyMs: lastSegment ? metrics?.latencyMs : undefined,
      });
    });
  }

  for (const ev of [...a2a].sort((a, b) => a.seq - b.seq)) {
    const p: Obj = isObj(ev.payload) ? ev.payload : {};
    if (ev.type === "quote_created") {
      const id = str(pick(p, ["quote_id", "id"]));
      if (id) mirrored.quotes.add(id);
    }
    if (ev.type === "transaction_created") {
      const id = str(pick(p, ["transaction_id", "id"]));
      if (id) mirrored.txns.add(id);
    }
    items.push({
      kind: "event",
      key: `a2a-${ev.seq}`,
      tsMs: parseTs(ev.ts),
      actor: a2aActor(ev),
      type: ev.type,
      ...describeEvent(ev.type, p),
    });
  }

  for (const run of runs) items.push(...milestonesOf(run, mirrored));

  // Items without a timestamp (a run that has not produced an event yet) are the newest thing.
  const sorted = items.map((it, i) => ({ it, i })).sort((a, b) => {
    const at = Number.isNaN(a.it.tsMs) ? Number.POSITIVE_INFINITY : a.it.tsMs;
    const bt = Number.isNaN(b.it.tsMs) ? Number.POSITIVE_INFINITY : b.it.tsMs;
    return at === bt ? a.i - b.i : at - bt;
  });
  return sorted.map((s) => s.it);
}
