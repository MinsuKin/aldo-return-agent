import type { Provenance, RunEvent } from "../api/types";
import { PRICE_IN_PER_MTOK_USD, PRICE_OUT_PER_MTOK_USD } from "../config";
import { fitHeadline } from "./fit";
import { formatDate, formatScalar } from "./format";
import { PURCHASE_OPTION_LABEL, provenanceKinds } from "./normalize";
import { humanize, isObj, num, pick, str } from "./util";

export type RunKind = "customer" | "approval" | "requote" | "personal";

export interface RunState {
  runId: string;
  kind: RunKind;
  status: string;
  events: RunEvent[];
}

export const isTerminal = (status: string | undefined): boolean => status === "completed" || status === "error";

export interface Chip {
  label: string;
  value: string;
}

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

export type A2aIcon = "signal" | "question" | "decision" | "transaction" | "memory" | "tool" | "check" | "message";

export type RowBody =
  | { kind: "run_started" }
  | {
      kind: "tool";
      tool: string;
      args: Chip[];
      pending: boolean;
      ok?: boolean;
      code?: string;
      message?: string;
      latencyMs?: number;
      evidence: Chip[];
      provenance: Provenance[];
    }
  | { kind: "decision"; text: string }
  | { kind: "milestone"; icon: "quote" | "approval" | "transaction" | "case"; label: string }
  | { kind: "assistant" }
  | { kind: "warning"; code: string; message: string }
  | { kind: "run_completed"; metrics: RunMetrics }
  | {
      kind: "a2a";
      eventType: string;
      icon: A2aIcon;
      headline: string;
      detail?: string;
      chips: Chip[];
      provenance: Provenance[];
    };

export type TraceRow = RowBody & { seq: number; actor: string };

type ToolRow = Extract<TraceRow, { kind: "tool" }>;

const FIT_TOOLS = new Set(["get_fit_insight"]);

/** Event types that belong to the agent-to-agent handshake (pivot addendum 5.6). */
export const A2A_TYPES = new Set([
  "size_assist_requested",
  "fit_signal_shared",
  "size_requested",
  "size_recommended",
  "size_confirmed",
  "memory_updated",
]);

export const DEFAULT_ACTOR: Record<string, string> = {
  size_assist_requested: "personal_agent",
  fit_signal_shared: "aldo_gateway",
  size_requested: "aldo_gateway",
  size_recommended: "personal_agent",
  size_confirmed: "aldo_gateway",
  memory_updated: "personal_agent",
};

export function actorOf(ev: RunEvent): string {
  const fromPayload = isObj(ev.payload) ? str(ev.payload.actor) : undefined;
  return ev.actor ?? fromPayload ?? DEFAULT_ACTOR[ev.type] ?? "aldo_agent";
}

function chips(o: unknown, skip: string[] = []): Chip[] {
  if (!isObj(o)) return [];
  return Object.entries(o)
    .filter(([k, v]) => !skip.includes(k) && v !== null && v !== undefined)
    .map(([k, v]) => ({ label: humanize(k), value: formatScalar(v) }));
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

/** Public-field description of an A2A handshake event. Tolerant of missing fields. */
export function describeA2a(type: string, p: Record<string, unknown>): Omit<Extract<RowBody, { kind: "a2a" }>, "kind" | "eventType"> {
  const product = str(pick(p, ["product_name", "product_query", "style"]));
  const color = str(p.color);
  const offered = Array.isArray(p.sizes)
    ? p.sizes.map((s) => (isObj(s) ? str(s.size) : str(s))).filter((s): s is string => !!s)
    : Array.isArray(p.offered_sizes)
      ? p.offered_sizes.map((s) => str(s)).filter((s): s is string => !!s)
      : [];

  switch (type) {
    case "size_assist_requested": {
      const deadline = str(p.deadline_date);
      return {
        icon: "question",
        headline: `Size help requested${product ? ` for ${product}` : ""}${color ? ` (${color.toLowerCase()})` : ""}`,
        detail: deadline ? `Deadline ${formatDate(deadline)}` : undefined,
        chips: [],
        provenance: [],
      };
    }
    case "fit_signal_shared": {
      const sig = isObj(p.fit_signal) ? p.fit_signal : p;
      const signal = str(sig.signal);
      const readable = signal ? signal.replace(/_/g, " ") : "signal";
      return {
        icon: "signal",
        headline: `Fit signal shared: ${readable}`,
        detail: fitHeadline(sig)?.replace(/^[^:]+: /, ""),
        chips: [],
        provenance: provenanceKinds(sig.provenance, p.provenance, "derived"),
      };
    }
    case "size_requested": {
      const question = str(p.question);
      return {
        icon: "question",
        headline: `Asked which size and option to reserve${offered.length ? ` (in stock: ${offered.join(", ")})` : ""}`,
        detail: question,
        chips: [],
        provenance: [],
      };
    }
    case "size_recommended": {
      const size = str(pick(p, ["chosen_size", "size"]));
      const conf = str(p.confidence);
      const type = str(p.option_type);
      const where = str(p.location_name);
      const how = type ? (PURCHASE_OPTION_LABEL[type] ?? humanize(type)).toLowerCase() : undefined;
      const why = str(pick(p, ["rationale_public", "rationale"]));
      return {
        icon: "decision",
        headline: `Proposed size ${size ?? "?"}${how ? `, ${how}` : ""}${where ? ` at ${where}` : ""}${conf ? ` (${conf} confidence)` : ""}`,
        detail: why,
        chips: [],
        provenance: [],
      };
    }
    case "size_confirmed": {
      const order = str(pick(p, ["order_id", "id"]));
      const size = str(pick(p, ["chosen_size", "size"])) ?? (isObj(p.line) ? str(p.line.size) : undefined);
      const next = str(p.next_step);
      const byOwner = str(p.approved_by) === "owner_click";
      return {
        icon: "check",
        headline: `${byOwner ? "Owner confirmed. " : ""}Simulated order ${order ?? ""} placed${size ? ` (size ${size})` : ""}`.replace(/\s+/g, " "),
        detail: next,
        chips: [],
        provenance: [],
      };
    }
    case "memory_updated": {
      const from = str(p.original_size);
      const to = str(p.new_size);
      const provisional = p.provisional === true;
      const outcome = str(p.outcome);
      return {
        icon: "memory",
        headline: `Memory updated${from && to ? `: ${from} to ${to}` : ""} (${provisional ? "provisional" : "confirmed"})`,
        detail: [product, outcome ? humanize(outcome).toLowerCase() : undefined].filter(Boolean).join(", ") || undefined,
        chips: [],
        provenance: [],
      };
    }
    default:
      return { icon: "message", headline: humanize(type), chips: chips(p), provenance: [] };
  }
}

/** Turn ordered events into display rows: tool_call + tool_result pairs are merged. */
export function buildTraceRows(events: RunEvent[]): TraceRow[] {
  const rows: TraceRow[] = [];
  const pending: ToolRow[] = [];
  const callIds = new Map<ToolRow, string>();

  for (const ev of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = isObj(ev.payload) ? ev.payload : {};
    const base = { seq: ev.seq, actor: actorOf(ev) };

    if (A2A_TYPES.has(ev.type)) {
      rows.push({ ...base, kind: "a2a", eventType: ev.type, ...describeA2a(ev.type, p) });
      continue;
    }

    switch (ev.type) {
      case "run_started":
        rows.push({ ...base, kind: "run_started" });
        break;
      case "tool_call": {
        const row: ToolRow = {
          ...base,
          kind: "tool",
          tool: str(pick(p, ["tool", "tool_name", "name"])) ?? "tool",
          args: chips(pick(p, ["args", "arguments", "input"])),
          pending: true,
          evidence: [],
          provenance: [],
        };
        const id = str(pick(p, ["call_id", "tool_use_id"]));
        if (id) callIds.set(row, id);
        rows.push(row);
        pending.push(row);
        break;
      }
      case "tool_result": {
        const tool = str(pick(p, ["tool", "tool_name", "name"])) ?? "tool";
        const id = str(pick(p, ["call_id", "tool_use_id"]));
        let idx = id ? pending.findIndex((r) => callIds.get(r) === id) : -1;
        if (idx < 0) idx = pending.findIndex((r) => r.tool === tool);
        let row: ToolRow;
        if (idx >= 0) {
          row = pending.splice(idx, 1)[0];
        } else {
          row = { ...base, kind: "tool", tool, args: [], pending: true, evidence: [], provenance: [] };
          rows.push(row);
        }
        const code = str(pick(p, ["code", "error_code"]));
        const okRaw = p.ok;
        const ok = typeof okRaw === "boolean" ? okRaw : !code;
        row.pending = false;
        row.ok = ok;
        row.code = ok ? undefined : (code ?? "ERROR");
        row.message = str(pick(p, ["message", "error_message"]));
        row.latencyMs = num(pick(p, ["latency_ms", "latencyMs"]));
        row.evidence = chips(p.evidence ?? p.data);
        const kinds = provenanceKinds(p.provenance, p.source_refs);
        row.provenance = kinds.length ? kinds : ok ? [FIT_TOOLS.has(row.tool) ? "derived" : "synthetic"] : [];
        break;
      }
      case "decision":
        rows.push({ ...base, kind: "decision", text: str(pick(p, ["summary", "text", "rationale"])) ?? "" });
        break;
      case "quote_created": {
        const id = str(pick(p, ["quote_id", "id"])) ?? "";
        const n = Array.isArray(p.option_ids) ? p.option_ids.length : num(pick(p, ["option_count"]));
        rows.push({
          ...base,
          kind: "milestone",
          icon: "quote",
          label: `Quote ${id} created${n !== undefined ? ` with ${n} option${n === 1 ? "" : "s"}` : ""}`,
        });
        break;
      }
      case "approval_received": {
        const opt = str(pick(p, ["option_id"])) ?? "";
        rows.push({ ...base, kind: "milestone", icon: "approval", label: `Customer approval received${opt ? ` for ${opt}` : ""}` });
        break;
      }
      case "transaction_created": {
        const id = str(pick(p, ["transaction_id", "id"])) ?? "";
        rows.push({ ...base, kind: "milestone", icon: "transaction", label: `${id} created (simulated)` });
        break;
      }
      case "case_created": {
        const id = str(pick(p, ["case_id", "id"])) ?? "";
        rows.push({ ...base, kind: "milestone", icon: "case", label: `Case ${id} opened for human review` });
        break;
      }
      case "assistant_message":
        rows.push({ ...base, kind: "assistant" });
        break;
      case "warning":
        rows.push({ ...base, kind: "warning", code: str(p.code) ?? "WARNING", message: str(p.message) ?? "" });
        break;
      case "run_completed":
        rows.push({ ...base, kind: "run_completed", metrics: buildMetrics(p) });
        break;
      default:
        break;
    }
  }
  return rows;
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
