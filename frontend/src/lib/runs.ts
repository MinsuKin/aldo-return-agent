import type { Provenance, RunEvent } from "../api/types";
import { PRICE_IN_PER_MTOK_USD, PRICE_OUT_PER_MTOK_USD } from "../config";
import { formatScalar } from "./format";
import { provenanceKinds } from "./normalize";
import { humanize, isObj, num, pick, str } from "./util";

/** "personal" = ALDO bot, purchase workflow. "returns" = ALDO bot, return workflow (addendum 11.5). */
export type RunAgent = "personal" | "returns";
export type RunKind = "message" | "approval" | "requote";

export interface RunState {
  runId: string;
  agent: RunAgent;
  kind: RunKind;
  status: string;
  events: RunEvent[];
}

export const isTerminal = (status: string | undefined): boolean => status === "completed" || status === "error";

export interface Chip {
  label: string;
  value: string;
}

export const AGENT_ACTOR: Record<RunAgent, string> = { personal: "personal_agent", returns: "aldo_agent" };

/** Actor of a run event: the event's own `actor` (API rows carry it), else the run's workflow. */
export function runEventActor(ev: RunEvent, run: Pick<RunState, "agent">): string {
  const fromPayload = isObj(ev.payload) ? str(ev.payload.actor) : undefined;
  return ev.actor ?? fromPayload ?? AGENT_ACTOR[run.agent];
}

// ---- Tool steps (the expandable sub-steps of a run in the trace) ------------------------------

/** Tools whose inputs and results are ALDO bot's own notes and memory: the trace only says that they ran. */
const PRIVATE_BOT_TOOLS = new Set(["recall_size_memory", "record_decision"]);
/** Chip keys that could carry the owner's private history; never shown for ALDO bot's own tools. */
const PRIVATE_KEY_RE = /typical|retailer|memory|history|owner|private|kept|returned_too|entries/i;

export const TOOL_LABEL: Record<string, string> = {
  recall_size_memory: "Recall the owner's size memory",
  ask_aldo_size_assist: "Ask ALDO for size help",
  read_product_reviews: "Read product reviews",
  send_size_decision: "Propose a size",
  hand_off_to_returns: "Switch to the return workflow",
  record_decision: "Note the decision",
  get_order_context: "Read the order",
  list_my_orders: "List the orders",
  evaluate_return_policy: "Check the return policy",
  get_fit_insight: "Check the fit signal",
  find_resolution_options: "Find exchange and return options",
  create_quote: "Prepare the options",
  execute_approved_resolution: "Carry out the confirmed option",
  get_resolution_status: "Check the saved record",
  escalate_case: "Send to a person for review",
};

export interface ToolStep {
  key: string;
  seq: number;
  /** Who made the call: ALDO bot's purchase workflow (personal_agent) or its return workflow (aldo_agent). */
  actor: string;
  /** When the call started (ms since epoch), NaN when the event has no timestamp. */
  tsMs: number;
  tool: string;
  label: string;
  args: Chip[];
  pending: boolean;
  ok?: boolean;
  code?: string;
  message?: string;
  latencyMs?: number;
  evidence: Chip[];
  provenance: Provenance[];
  /** Private to ALDO bot: only the fact that the tool ran is shown. */
  hidden: boolean;
}

function chips(o: unknown, skip: string[] = []): Chip[] {
  if (!isObj(o)) return [];
  return Object.entries(o)
    .filter(([k, v]) => !skip.includes(k) && v !== null && v !== undefined)
    .map(([k, v]) => ({ label: humanize(k), value: formatScalar(v) }));
}

function publicChips(o: unknown): Chip[] {
  if (!isObj(o)) return [];
  const safe: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) if (!PRIVATE_KEY_RE.test(k)) safe[k] = v;
  return chips(safe);
}

const tsOf = (ts: string | undefined): number => {
  const t = ts ? Date.parse(ts) : Number.NaN;
  return Number.isNaN(t) ? Number.NaN : t;
};

/** Pair tool_call and tool_result events of a run into steps, in call order. */
export function toolSteps(run: RunState): ToolStep[] {
  const steps: ToolStep[] = [];
  const pending: ToolStep[] = [];
  const callIds = new Map<ToolStep, string>();

  for (const ev of [...run.events].sort((a, b) => a.seq - b.seq)) {
    const p = isObj(ev.payload) ? ev.payload : {};
    const actor = runEventActor(ev, run);
    // ALDO bot's purchase workflow keeps its notes and memory private; the return workflow shows server facts.
    const personal = actor === "personal_agent";
    if (ev.type === "tool_call") {
      const tool = str(pick(p, ["tool", "tool_name", "name"])) ?? "tool";
      const hidden = personal && PRIVATE_BOT_TOOLS.has(tool);
      const rawArgs = pick(p, ["args", "arguments", "input"]);
      const step: ToolStep = {
        key: `${run.runId}-${ev.seq}`,
        seq: ev.seq,
        actor,
        tsMs: tsOf(ev.ts),
        tool,
        label: TOOL_LABEL[tool] ?? humanize(tool),
        args: hidden ? [] : personal ? publicChips(rawArgs) : chips(rawArgs),
        pending: true,
        evidence: [],
        provenance: [],
        hidden,
      };
      const id = str(pick(p, ["call_id", "tool_use_id"]));
      if (id) callIds.set(step, id);
      steps.push(step);
      pending.push(step);
    } else if (ev.type === "tool_result") {
      const tool = str(pick(p, ["tool", "tool_name", "name"])) ?? "tool";
      const id = str(pick(p, ["call_id", "tool_use_id"]));
      let idx = id ? pending.findIndex((r) => callIds.get(r) === id) : -1;
      if (idx < 0) idx = pending.findIndex((r) => r.tool === tool);
      let step: ToolStep;
      if (idx >= 0) {
        step = pending.splice(idx, 1)[0];
      } else {
        step = {
          key: `${run.runId}-${ev.seq}`,
          seq: ev.seq,
          actor,
          tsMs: tsOf(ev.ts),
          tool,
          label: TOOL_LABEL[tool] ?? humanize(tool),
          args: [],
          pending: true,
          evidence: [],
          provenance: [],
          hidden: personal && PRIVATE_BOT_TOOLS.has(tool),
        };
        steps.push(step);
      }
      const code = str(pick(p, ["code", "error_code"]));
      const okRaw = p.ok;
      const ok = typeof okRaw === "boolean" ? okRaw : !code;
      step.pending = false;
      step.ok = ok;
      step.code = ok ? undefined : (code ?? "ERROR");
      step.message = ok ? undefined : str(pick(p, ["message", "error_message"]));
      step.latencyMs = num(pick(p, ["latency_ms", "latencyMs"]));
      if (!step.hidden) {
        const raw = p.evidence ?? p.data;
        step.evidence = personal ? publicChips(raw) : chips(raw);
        step.provenance = provenanceKinds(p.provenance, p.source_refs).filter((k) => k !== "synthetic");
      }
    }
  }
  return steps;
}

// ---- Run metrics ------------------------------------------------------------------------------

export interface RunMetrics {
  status: string;
  rounds?: number;
  tools?: number;
  inputTokens?: number;
  outputTokens?: number;
  latencyMs?: number;
  modelId?: string;
  errorCode?: string;
  cost?: { amount: number; currency: string; assumed: boolean };
}

export function buildMetrics(payload: Record<string, unknown>): RunMetrics {
  const inputTokens = num(pick(payload, ["input_tokens", "inputTokens"]));
  const outputTokens = num(pick(payload, ["output_tokens", "outputTokens"]));
  let cost: RunMetrics["cost"];
  const reportedUsd = num(pick(payload, ["estimated_cost_usd", "est_cost_usd", "cost_usd"]));
  const reportedCad = num(pick(payload, ["estimated_cost_cad", "est_cost_cad", "cost_cad"]));
  if (reportedCad !== undefined) cost = { amount: reportedCad, currency: "CAD", assumed: true };
  else if (reportedUsd !== undefined) cost = { amount: reportedUsd, currency: "USD", assumed: true };
  else if (inputTokens !== undefined || outputTokens !== undefined) {
    const usd =
      ((inputTokens ?? 0) * PRICE_IN_PER_MTOK_USD) / 1_000_000 + ((outputTokens ?? 0) * PRICE_OUT_PER_MTOK_USD) / 1_000_000;
    cost = { amount: usd, currency: "USD", assumed: true };
  }
  return {
    status: str(payload.status) ?? "completed",
    rounds: num(pick(payload, ["rounds", "model_rounds"])),
    tools: num(pick(payload, ["tool_count", "tools", "tool_calls"])),
    inputTokens,
    outputTokens,
    latencyMs: num(pick(payload, ["total_latency_ms", "latency_ms", "duration_ms"])),
    modelId: str(pick(payload, ["model_id_used", "model_id", "model"])),
    errorCode: str(payload.error_code),
    cost,
  };
}

/** The run_completed metrics of a run, when it has finished. */
export function runMetrics(run: RunState): RunMetrics | undefined {
  const done = [...run.events].reverse().find((e) => e.type === "run_completed");
  return done && isObj(done.payload) ? buildMetrics(done.payload) : undefined;
}

/** Distinct ids carried in `fields` of events of `type`, across runs in order. */
export function allFromEvents(runs: RunState[], type: string, fields: string[]): string[] {
  const out: string[] = [];
  for (const run of runs) {
    for (const ev of [...run.events].sort((a, b) => a.seq - b.seq)) {
      if (ev.type !== type) continue;
      const v = str(pick(ev.payload, fields));
      if (v && !out.includes(v)) out.push(v);
    }
  }
  return out;
}
