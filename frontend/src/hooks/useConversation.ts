import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RunEvent, SendMessageResponse } from "../api/types";
import { DELIVERY_DELAY_MS } from "../config";
import { describeError, type ErrorInfo } from "../lib/errors";
import type { OptionView, PurchaseView, QuoteView } from "../lib/normalize";
import { isTerminal, type RunAgent, type RunState } from "../lib/runs";
import { norm } from "../lib/script";
import { convReducer, entriesOf, initialConv, type Entry, type Mode } from "../state/conversation";
import { useRunPolling } from "./useRunPolling";

export type DeliveryStatus = "out" | "delivered" | "error";
export interface Delivery {
  status: DeliveryStatus;
  startedAt: number;
  product?: string;
  size?: string;
  error?: string;
}

export interface PushNote {
  id: number;
  title: string;
  body: string;
}

export interface Conversation {
  entries: Entry[];
  runs: RunState[];
  mode: Mode;
  /** A run is active or a message is being sent. */
  busy: boolean;
  activeAgent: RunAgent | null;
  sendError: ErrorInfo | null;
  clearSendError: () => void;
  send: (text: string) => Promise<boolean>;
  /** Owner clicked Confirm on a return option. Throws ApiError; the dialog shows it. */
  approve: (quoteId: string, option: OptionView) => Promise<void>;
  /** Owner clicked Confirm purchase. Throws ApiError; the dialog shows it. */
  confirmPurchase: (view: PurchaseView) => Promise<void>;
  requote: (quote: QuoteView) => Promise<void>;
  lastUserText: () => string | undefined;
  deliveries: Record<string, Delivery>;
  retryDelivery: (orderId: string) => void;
  push: PushNote | null;
  dismissPush: () => void;
  userTexts: string[];
  delivered: boolean;
  confirmedCount: number;
}

const PUSH_VISIBLE_MS = 6000;

function agentOf(res: SendMessageResponse): RunAgent {
  if (res.agent === "returns") return "returns";
  if (res.agent === "personal") return "personal";
  if (res.mode === "returns") return "returns";
  return res.run_id.startsWith("PRUN-") ? "personal" : "returns";
}

/**
 * The one chat with the personal agent. It owns the transcript, the active run (polled), the purchase delivery
 * sequence (addendum 11.1 D10) and the push notification. `a2a` is the shared A2A feed; `onDelivered`
 * asks that feed to poll quickly because delivery adds an event to it.
 */
export function useConversation(a2a: RunEvent[], onDelivered: () => void): Conversation {
  const qc = useQueryClient();
  const [state, dispatch] = useReducer(convReducer, initialConv);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<ErrorInfo | null>(null);
  const [deliveries, setDeliveries] = useState<Record<string, Delivery>>({});
  const [push, setPush] = useState<PushNote | null>(null);

  const stateRef = useRef(state);
  const a2aRef = useRef(a2a);
  const started = useRef(new Set<string>());
  const timers = useRef<number[]>([]);
  useEffect(() => {
    stateRef.current = state;
    a2aRef.current = a2a;
  });
  useEffect(
    () => () => {
      timers.current.forEach((t) => window.clearTimeout(t));
      timers.current = [];
    },
    [],
  );

  const onEvents = useCallback((runId: string, events: RunEvent[], status: string) => dispatch({ type: "events", runId, events, status }), []);
  useRunPolling(state.activeRunId, onEvents);

  const entries = useMemo(() => entriesOf(state, a2a), [state, a2a]);

  // The conversation mode flips when the personal agent switches workflows; read it after every finished run and
  // whenever the A2A stream shows a hand-off or hand-back.
  const finished = state.runs.filter((r) => isTerminal(r.status)).length;
  const modeSignal = useMemo(() => a2a.filter((e) => e.type === "handoff_to_returns" || e.type === "handback").length, [a2a]);
  useEffect(() => {
    const convId = state.convId;
    if (!convId) return;
    let cancelled = false;
    api
      .paConversation(convId)
      .then((info) => {
        if (!cancelled) dispatch({ type: "mode", mode: info.mode === "returns" ? "returns" : "bot", returnsOrderId: info.returns_order_id ?? null });
      })
      .catch(() => {
        if (cancelled) return;
        const last = [...a2aRef.current].reverse().find((e) => e.type === "handoff_to_returns" || e.type === "handback");
        if (last) dispatch({ type: "mode", mode: last.type === "handoff_to_returns" ? "returns" : "bot", returnsOrderId: null });
      });
    return () => {
      cancelled = true;
    };
  }, [state.convId, finished, modeSignal]);

  // A finished run may have recorded a proposal or options, or changed what the personal agent remembers.
  useEffect(() => {
    if (finished > 0) {
      void qc.invalidateQueries({ queryKey: ["purchase"] });
      void qc.invalidateQueries({ queryKey: ["quote"] });
      void qc.invalidateQueries({ queryKey: ["txn"] });
      void qc.invalidateQueries({ queryKey: ["pa-memory"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
    }
  }, [finished, qc]);

  const send = useCallback(async (text: string): Promise<boolean> => {
    setSending(true);
    setSendError(null);
    try {
      let convId = stateRef.current.convId;
      if (!convId) {
        const created = await api.paCreateConversation();
        convId = created.conv_id;
        dispatch({ type: "conversation", convId });
      }
      const res = await api.paSendMessage(convId, text);
      dispatch({ type: "user", text });
      dispatch({ type: "run_started", runId: res.run_id, agent: agentOf(res), kind: "message" });
      return true;
    } catch (e) {
      setSendError(describeError(e));
      return false;
    } finally {
      setSending(false);
    }
  }, []);

  const approve = useCallback(
    async (quoteId: string, option: OptionView) => {
      const res = await api.approve(quoteId, option.optionId, option.payloadHash);
      const what = option.lines.length > 1 ? `the return of ${option.lines.length} items` : option.isExchange ? "the exchange" : "the return";
      dispatch({ type: "notice", text: `You confirmed ${what}.` });
      dispatch({ type: "run_started", runId: res.run_id, agent: "returns", kind: "approval" });
      void qc.invalidateQueries({ queryKey: ["quote"] });
    },
    [qc],
  );

  const deliver = useCallback(
    async (orderId: string, product: string | undefined, size: string | undefined) => {
      try {
        await api.simulateDelivery(orderId);
        setDeliveries((d) => ({ ...d, [orderId]: { ...(d[orderId] ?? { startedAt: Date.now() }), status: "delivered", product, size } }));
        dispatch({
          type: "bot",
          text: `Your ${product ?? "order"}${size ? ` (size ${size})` : ""} was delivered. Tell me if it works out, or if you want to send it back.`,
        });
        setPush({ id: Date.now(), title: "Personal agent", body: `Delivered: ${product ?? "your order"}${size ? `, size ${size}` : ""}` });
        onDelivered();
        void qc.invalidateQueries({ queryKey: ["orders"] });
      } catch (e) {
        setDeliveries((d) => ({
          ...d,
          [orderId]: { ...(d[orderId] ?? { startedAt: Date.now() }), status: "error", product, size, error: describeError(e).message },
        }));
      }
    },
    [qc, onDelivered],
  );

  const confirmPurchase = useCallback(
    async (view: PurchaseView) => {
      if (!view.optionId) throw new Error("This proposal has no option to confirm yet.");
      const res = await api.confirmPurchase(view.sizeRequestId, view.optionId);
      dispatch({ type: "order", orderId: res.order_id, sizeRequestId: view.sizeRequestId });
      void qc.invalidateQueries({ queryKey: ["purchase"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
      if (started.current.has(res.order_id)) return;
      started.current.add(res.order_id);
      setDeliveries((d) => ({ ...d, [res.order_id]: { status: "out", startedAt: Date.now(), product: view.productName, size: view.size } }));
      timers.current.push(window.setTimeout(() => void deliver(res.order_id, view.productName, view.size), DELIVERY_DELAY_MS));
    },
    [qc, deliver],
  );

  const retryDelivery = useCallback(
    (orderId: string) => {
      const cur = deliveries[orderId];
      setDeliveries((d) => ({ ...d, [orderId]: { ...(d[orderId] ?? { startedAt: Date.now() }), status: "out", error: undefined } }));
      void deliver(orderId, cur?.product, cur?.size);
    },
    [deliveries, deliver],
  );

  const requote = useCallback(
    async (quote: QuoteView) => {
      if (!quote.convId) {
        await send("Please refresh my options.");
        return;
      }
      const res = await api.requote(quote.convId, quote.quoteId);
      dispatch({ type: "notice", text: "You asked for fresh options." });
      dispatch({ type: "run_started", runId: res.run_id, agent: "returns", kind: "requote" });
    },
    [send],
  );

  useEffect(() => {
    if (!push) return;
    const t = window.setTimeout(() => setPush((cur) => (cur?.id === push.id ? null : cur)), PUSH_VISIBLE_MS);
    return () => window.clearTimeout(t);
  }, [push]);

  const userTexts = useMemo(() => state.items.filter((i) => i.type === "user").map((i) => norm(i.text)), [state.items]);
  const activeRun = state.runs.find((r) => r.runId === state.activeRunId);

  return {
    entries,
    runs: state.runs,
    mode: state.mode,
    busy: state.activeRunId !== null || sending,
    activeAgent: activeRun?.agent ?? null,
    sendError,
    clearSendError: () => setSendError(null),
    send,
    approve,
    confirmPurchase,
    requote,
    lastUserText: () => [...stateRef.current.items].reverse().find((i) => i.type === "user")?.text,
    deliveries,
    retryDelivery,
    push,
    dismissPush: () => setPush(null),
    userTexts,
    delivered: Object.values(deliveries).some((d) => d.status === "delivered"),
    confirmedCount: entries.filter((e) => e.kind === "card" && e.card.type === "txn").length,
  };
}
