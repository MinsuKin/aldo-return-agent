import type { RunEvent } from "../api/types";
import { isTerminal, type RunAgent, type RunKind, type RunState } from "../lib/runs";
import { isObj, pick, str, type Obj } from "../lib/util";

// One chat with the personal agent (addendum 11.1 D1/D3). The transcript is the owner's messages, the personal agent's
// replies and inline cards. Cards only carry an id: their content is always read from the server.

export type CardRef =
  | { type: "purchase"; sizeRequestId: string }
  | { type: "quote"; quoteId: string }
  | { type: "order"; orderId: string; sizeRequestId?: string }
  | { type: "txn"; transactionId: string }
  | { type: "case"; caseId: string };

export type Entry =
  | { key: string; kind: "user"; text: string }
  | { key: string; kind: "bot"; text: string }
  | { key: string; kind: "notice"; text: string }
  | { key: string; kind: "card"; card: CardRef };

type Item =
  | { key: string; type: "user"; text: string }
  | { key: string; type: "run"; runId: string }
  | { key: string; type: "notice"; text: string }
  | { key: string; type: "bot"; text: string }
  | { key: string; type: "order"; orderId: string; sizeRequestId?: string };

export type Mode = "bot" | "returns";

export interface ConvState {
  convId: string | null;
  mode: Mode;
  returnsOrderId: string | null;
  items: Item[];
  runs: RunState[];
  activeRunId: string | null;
  counter: number;
}

export const initialConv: ConvState = { convId: null, mode: "bot", returnsOrderId: null, items: [], runs: [], activeRunId: null, counter: 0 };

export type ConvAction =
  | { type: "conversation"; convId: string }
  | { type: "mode"; mode: Mode; returnsOrderId: string | null }
  | { type: "user"; text: string }
  | { type: "notice"; text: string }
  | { type: "bot"; text: string }
  | { type: "order"; orderId: string; sizeRequestId?: string }
  | { type: "run_started"; runId: string; agent: RunAgent; kind: RunKind }
  | { type: "events"; runId: string; events: RunEvent[]; status: string };

export function convReducer(state: ConvState, action: ConvAction): ConvState {
  switch (action.type) {
    case "conversation":
      return { ...state, convId: action.convId };

    case "mode":
      return state.mode === action.mode && state.returnsOrderId === action.returnsOrderId ? state : { ...state, mode: action.mode, returnsOrderId: action.returnsOrderId };

    case "user":
    case "notice":
    case "bot": {
      const counter = state.counter + 1;
      const item: Item = { key: `${action.type}-${counter}`, type: action.type, text: action.text };
      return { ...state, counter, items: [...state.items, item] };
    }

    case "order": {
      if (state.items.some((i) => i.type === "order" && i.orderId === action.orderId)) return state;
      const counter = state.counter + 1;
      return { ...state, counter, items: [...state.items, { key: `order-${counter}`, type: "order", orderId: action.orderId, sizeRequestId: action.sizeRequestId }] };
    }

    case "run_started": {
      if (state.runs.some((r) => r.runId === action.runId)) {
        // Replay (for example a double click on Confirm): keep following the same run.
        return state.activeRunId === action.runId ? state : { ...state, activeRunId: state.activeRunId ?? action.runId };
      }
      const run: RunState = { runId: action.runId, agent: action.agent, kind: action.kind, status: "queued", events: [] };
      const counter = state.counter + 1;
      return {
        ...state,
        counter,
        runs: [...state.runs, run],
        activeRunId: action.runId,
        items: [...state.items, { key: `run-${counter}`, type: "run", runId: action.runId }],
      };
    }

    case "events": {
      const idx = state.runs.findIndex((r) => r.runId === action.runId);
      if (idx < 0) return state;
      const run = state.runs[idx];
      const bySeq = new Map<number, RunEvent>(run.events.map((e) => [e.seq, e]));
      let changed = false;
      for (const ev of action.events) {
        if (!bySeq.has(ev.seq)) {
          bySeq.set(ev.seq, ev);
          changed = true;
        }
      }
      const status = action.status || run.status;
      if (!changed && status === run.status) return state;
      const runs = state.runs.slice();
      runs[idx] = { ...run, events: [...bySeq.values()].sort((a, b) => a.seq - b.seq), status };
      const activeRunId = state.activeRunId === action.runId && isTerminal(status) ? null : state.activeRunId;
      return { ...state, runs, activeRunId };
    }

    default:
      return state;
  }
}

// ---- Transcript derivation -----------------------------------------------------------------------

const cardId = (c: CardRef): string => {
  switch (c.type) {
    case "purchase":
      return `purchase:${c.sizeRequestId}`;
    case "quote":
      return `quote:${c.quoteId}`;
    case "order":
      return `order:${c.orderId}`;
    case "txn":
      return `txn:${c.transactionId}`;
    case "case":
      return `case:${c.caseId}`;
  }
};

const ID_OK = /^[A-Za-z0-9._:-]{1,128}$/;
const safeId = (v: string | undefined): string | undefined => (v && ID_OK.test(v) ? v : undefined);

function tsOf(ev: RunEvent | undefined): number {
  const t = ev?.ts ? Date.parse(ev.ts) : Number.NaN;
  return Number.isNaN(t) ? Number.NaN : t;
}

/**
 * What one run adds to the chat: the personal agent's replies, each followed by the cards the run produced before
 * it (a proposal, options, a confirmation). Cards wait for the reply that introduces them.
 */
function runEntries(run: RunState, a2a: RunEvent[]): Entry[] {
  const out: Entry[] = [];
  const pending: CardRef[] = [];
  const queued = new Set<string>();
  const events = [...run.events].sort((a, b) => a.seq - b.seq);
  const lastCallArgs = new Map<string, Obj>();
  let sawReply = false;

  const queue = (c: CardRef) => {
    const id = cardId(c);
    if (queued.has(id)) return;
    queued.add(id);
    pending.push(c);
  };
  const flush = () => {
    for (const c of pending.splice(0)) out.push({ key: `${run.runId}-${cardId(c)}`, kind: "card", card: c });
  };

  for (const ev of events) {
    const p: Obj = isObj(ev.payload) ? ev.payload : {};
    switch (ev.type) {
      case "assistant_message": {
        const text = str(pick(p, ["text", "message", "content"]));
        if (text) {
          out.push({ key: `${run.runId}-b${ev.seq}`, kind: "bot", text });
          sawReply = true;
        }
        flush();
        break;
      }
      case "tool_call": {
        const tool = str(pick(p, ["tool", "tool_name", "name"]));
        const args = pick(p, ["args", "arguments", "input"]);
        if (tool && isObj(args)) lastCallArgs.set(tool, args);
        break;
      }
      case "tool_result": {
        if (str(pick(p, ["tool", "tool_name", "name"])) === "send_size_decision" && p.ok !== false) {
          const evidence = isObj(p.evidence) ? p.evidence : {};
          const id = safeId(str(evidence.size_request_id) ?? str(lastCallArgs.get("send_size_decision")?.size_request_id));
          if (id) queue({ type: "purchase", sizeRequestId: id });
        }
        break;
      }
      case "size_recommended": {
        const id = safeId(str(p.size_request_id));
        if (id) queue({ type: "purchase", sizeRequestId: id });
        break;
      }
      case "quote_created": {
        const id = safeId(str(pick(p, ["quote_id", "id"])));
        if (id) queue({ type: "quote", quoteId: id });
        break;
      }
      case "transaction_created": {
        const id = safeId(str(pick(p, ["transaction_id", "id"])));
        if (id) queue({ type: "txn", transactionId: id });
        break;
      }
      case "case_created": {
        const id = safeId(str(pick(p, ["case_id", "id"])));
        if (id) queue({ type: "case", caseId: id });
        break;
      }
      default:
        break;
    }
  }

  // Fallback for servers that only publish the proposal on the A2A stream: a size_recommended event
  // that landed while this run was running belongs to it.
  if (run.agent === "personal" && !queued.size) {
    const start = tsOf(events[0]);
    const end = tsOf(events[events.length - 1]);
    if (!Number.isNaN(start)) {
      const hit = [...a2a]
        .reverse()
        .find((e) => e.type === "size_recommended" && tsOf(e) >= start - 1000 && tsOf(e) <= (Number.isNaN(end) ? Date.now() : end) + 3000);
      const id = hit && isObj(hit.payload) ? safeId(str(hit.payload.size_request_id)) : undefined;
      if (id) queue({ type: "purchase", sizeRequestId: id });
    }
  }

  if (isTerminal(run.status)) {
    flush();
    if (!sawReply && run.status === "error") {
      out.push({ key: `${run.runId}-err`, kind: "notice", text: "Your personal agent could not finish that. Please try again." });
    }
  }
  return out;
}

export function entriesOf(state: ConvState, a2a: RunEvent[]): Entry[] {
  const out: Entry[] = [];
  const runs = new Map(state.runs.map((r) => [r.runId, r]));
  const seen = new Set<string>();
  const add = (e: Entry) => {
    if (e.kind === "card") {
      const id = cardId(e.card);
      if (seen.has(id)) return;
      seen.add(id);
    }
    out.push(e);
  };

  for (const item of state.items) {
    switch (item.type) {
      case "user":
        add({ key: item.key, kind: "user", text: item.text });
        break;
      case "notice":
        add({ key: item.key, kind: "notice", text: item.text });
        break;
      case "bot":
        add({ key: item.key, kind: "bot", text: item.text });
        break;
      case "order":
        add({ key: item.key, kind: "card", card: { type: "order", orderId: item.orderId, sizeRequestId: item.sizeRequestId } });
        break;
      case "run": {
        const run = runs.get(item.runId);
        if (run) for (const e of runEntries(run, a2a)) add(e);
        break;
      }
    }
  }
  return out;
}
