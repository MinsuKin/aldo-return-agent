import type { RunEvent } from "../api/types";
import { isTerminal, type RunKind, type RunState } from "../lib/events";
import { isObj, pick, str } from "../lib/util";

export interface TranscriptEntry {
  key: string;
  role: "customer" | "assistant" | "action";
  text: string;
}

export interface ChatState {
  convId: string | null;
  entries: TranscriptEntry[];
  runs: RunState[];
  activeRunId: string | null;
  counter: number;
}

export const initialChat: ChatState = { convId: null, entries: [], runs: [], activeRunId: null, counter: 0 };

export type ChatAction =
  | { type: "conversation"; convId: string }
  | { type: "customer"; text: string }
  | { type: "action"; text: string }
  | { type: "run_started"; runId: string; kind: RunKind }
  | { type: "events"; runId: string; events: RunEvent[]; status: string };

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "conversation":
      return { ...state, convId: action.convId };

    case "customer":
    case "action": {
      const counter = state.counter + 1;
      const entry: TranscriptEntry = {
        key: `${action.type}-${counter}`,
        role: action.type,
        text: action.text,
      };
      return { ...state, counter, entries: [...state.entries, entry] };
    }

    case "run_started": {
      if (state.runs.some((r) => r.runId === action.runId)) {
        // Replay (for example a double click on Approve): keep following the same run.
        return state.activeRunId === action.runId ? state : { ...state, activeRunId: state.activeRunId ?? action.runId };
      }
      const run: RunState = { runId: action.runId, kind: action.kind, status: "queued", events: [] };
      return { ...state, runs: [...state.runs, run], activeRunId: action.runId };
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

      const events = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      const known = new Set(state.entries.map((e) => e.key));
      const added: TranscriptEntry[] = [];
      for (const ev of events) {
        if (ev.type !== "assistant_message") continue;
        const key = `assistant-${action.runId}-${ev.seq}`;
        if (known.has(key)) continue;
        const payload = isObj(ev.payload) ? ev.payload : {};
        const text = str(pick(payload, ["text", "message", "content"]));
        if (text) added.push({ key, role: "assistant", text });
      }

      const runs = state.runs.slice();
      runs[idx] = { ...run, events, status };
      const activeRunId = state.activeRunId === action.runId && isTerminal(status) ? null : state.activeRunId;
      return { ...state, runs, activeRunId, entries: added.length ? [...state.entries, ...added] : state.entries };
    }

    default:
      return state;
  }
}
