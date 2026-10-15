import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RunEvent } from "../api/types";
import { useRunPolling } from "../hooks/useRunPolling";
import { describeError, type ErrorInfo } from "../lib/errors";
import { allFromEvents, buildTraceRows, isTerminal, type RunKind } from "../lib/events";
import { normalizeCase, normalizeQuote, normalizeTransaction, type OptionView } from "../lib/normalize";
import { aldoPrompts, type SuggestedPrompt } from "../lib/prompts";
import { chatReducer, initialChat } from "../state/chat";
import { ApproveDialog } from "./ApproveDialog";
import { Chat } from "./Chat";
import { OptionCards } from "./OptionCards";
import { OrderList } from "./OrderList";
import { CasePanel, ConfirmationPanel } from "./ResultPanels";
import { TraceTimeline, type TraceGroup } from "./TraceTimeline";

const RUN_LABEL: Record<RunKind, string> = {
  customer: "your message",
  approval: "your approval",
  requote: "refreshing your options",
  personal: "personal agent",
};

interface Props {
  onActive: (active: boolean) => void;
  onRestart: () => void;
}

/** The ALDO Returns Agent: orders, chat, option cards, approval, confirmation, tool trace. */
export function AldoPanel({ onActive, onRestart }: Props) {
  const qc = useQueryClient();
  const [chat, dispatch] = useReducer(chatReducer, initialChat);
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<ErrorInfo | null>(null);
  const [target, setTarget] = useState<{ quoteId: string; option: OptionView } | null>(null);
  const [approveError, setApproveError] = useState<ErrorInfo | null>(null);
  const [deliveryBusyId, setDeliveryBusyId] = useState<string | null>(null);
  const [deliveryError, setDeliveryError] = useState<string | null>(null);
  const [pickupDone, setPickupDone] = useState(false);
  const [pickupError, setPickupError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  const orders = useQuery({ queryKey: ["orders"], queryFn: api.orders, retry: 1 });

  const onEvents = useCallback(
    (runId: string, events: RunEvent[], status: string) => dispatch({ type: "events", runId, events, status }),
    [],
  );
  useRunPolling(chat.activeRunId, onEvents);

  const runActive = chat.activeRunId !== null;
  useEffect(() => {
    onActive(runActive);
  }, [runActive, onActive]);

  // Ids surfaced by trace events. Everything shown to the customer is then read from the server.
  const quoteIds = useMemo(() => allFromEvents(chat.runs, "quote_created", ["quote_id", "id"]), [chat.runs]);
  const txnIds = useMemo(() => allFromEvents(chat.runs, "transaction_created", ["transaction_id", "id"]), [chat.runs]);
  const caseIds = useMemo(() => allFromEvents(chat.runs, "case_created", ["case_id", "id"]), [chat.runs]);
  const approvalCount = useMemo(
    () => chat.runs.reduce((n, r) => n + r.events.filter((e) => e.type === "approval_received").length, 0),
    [chat.runs],
  );
  const latestQuoteId = quoteIds.at(-1);
  const latestTxnId = txnIds.at(-1);
  const latestCaseId = caseIds.at(-1);

  const quoteQuery = useQuery({
    queryKey: ["quote", latestQuoteId],
    enabled: !!latestQuoteId,
    queryFn: () => api.quote(latestQuoteId as string),
    retry: 2,
  });
  const txnQuery = useQuery({
    queryKey: ["txn", latestTxnId],
    enabled: !!latestTxnId,
    queryFn: () => api.transaction(latestTxnId as string),
    retry: 3,
  });
  const casesQuery = useQuery({ queryKey: ["cases"], enabled: !!latestCaseId, queryFn: api.cases, retry: 2 });

  const quote = useMemo(() => {
    if (!quoteQuery.data) return undefined;
    const line = orders.data
      ?.find((o) => o.order_id === quoteQuery.data.order_id)
      ?.lines.find((l) => l.line_id === quoteQuery.data.line_id);
    return normalizeQuote(quoteQuery.data, { productName: line?.product_name, color: line?.color });
  }, [quoteQuery.data, orders.data]);
  const txn = useMemo(() => (txnQuery.data ? normalizeTransaction(txnQuery.data) : undefined), [txnQuery.data]);
  const caseView = useMemo(() => {
    const raw = casesQuery.data?.find((c) => c.case_id === latestCaseId);
    return raw ? normalizeCase(raw) : undefined;
  }, [casesQuery.data, latestCaseId]);

  // Server-side quote, order and inventory state changes on approval and execution, so re-read it then.
  useEffect(() => {
    if (approvalCount > 0 || txnIds.length > 0) {
      void qc.invalidateQueries({ queryKey: ["quote"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
    }
  }, [approvalCount, txnIds.length, qc]);
  useEffect(() => {
    if (caseIds.length > 0) void qc.invalidateQueries({ queryKey: ["cases"] });
  }, [caseIds.length, qc]);
  useEffect(() => {
    setPickupDone(false);
    setPickupError(null);
  }, [latestTxnId]);

  const send = useCallback(
    async (text: string, orderId: string | null): Promise<boolean> => {
      setSending(true);
      setSendError(null);
      try {
        let convId = chat.convId;
        if (!convId) {
          const created = await api.createConversation(orderId ?? undefined);
          convId = created.conv_id;
          dispatch({ type: "conversation", convId });
        }
        const res = await api.sendMessage(convId, text, orderId ?? undefined);
        dispatch({ type: "customer", text });
        dispatch({ type: "run_started", runId: res.run_id, kind: "customer" });
        return true;
      } catch (e) {
        setSendError(describeError(e));
        return false;
      } finally {
        setSending(false);
      }
    },
    [chat.convId],
  );

  const prompts = useMemo(() => aldoPrompts(orders.data), [orders.data]);

  function onSuggest(p: SuggestedPrompt) {
    let orderId = selectedOrderId;
    const wanted = p.orderId ?? (orders.data ? p.pickOrder?.(orders.data) : undefined);
    if (wanted && orders.data?.some((o) => o.order_id === wanted)) {
      orderId = wanted;
      setSelectedOrderId(wanted);
    }
    void send(p.text, orderId);
  }

  const requote = useMutation({
    mutationFn: (v: { convId: string; quoteId: string }) => api.requote(v.convId, v.quoteId),
    onSuccess: (res) => {
      setTarget(null);
      setApproveError(null);
      setSendError(null);
      dispatch({ type: "action", text: "You asked for fresh options." });
      dispatch({ type: "run_started", runId: res.run_id, kind: "requote" });
    },
    onError: (e) => setSendError(describeError(e)),
  });

  function requoteNow() {
    if (chat.convId && latestQuoteId) requote.mutate({ convId: chat.convId, quoteId: latestQuoteId });
  }

  const approve = useMutation({
    mutationFn: (v: { quoteId: string; option: OptionView }) => api.approve(v.quoteId, v.option.optionId, v.option.payloadHash),
    onSuccess: (res, v) => {
      setTarget(null);
      setApproveError(null);
      dispatch({ type: "action", text: `You approved: ${v.option.typeLabel}${v.option.locationName ? ` at ${v.option.locationName}` : ""}.` });
      dispatch({ type: "run_started", runId: res.run_id, kind: "approval" });
      void qc.invalidateQueries({ queryKey: ["quote"] });
      window.setTimeout(() => document.getElementById("options-title")?.focus(), 0);
    },
    onError: (e) => {
      setApproveError(describeError(e));
      void qc.invalidateQueries({ queryKey: ["quote"] });
    },
  });

  const delivery = useMutation({
    mutationFn: (orderId: string) => api.simulateDelivery(orderId),
    onMutate: (orderId) => {
      setDeliveryBusyId(orderId);
      setDeliveryError(null);
    },
    onSuccess: async (_res, orderId) => {
      await qc.invalidateQueries({ queryKey: ["orders"] });
      setSelectedOrderId(orderId);
    },
    onError: (e) => setDeliveryError(describeError(e).message),
    onSettled: () => setDeliveryBusyId(null),
  });

  const pickup = useMutation({
    mutationFn: (transactionId: string) => api.simulatePickup(transactionId),
    onMutate: () => setPickupError(null),
    onSuccess: () => setPickupDone(true),
    onError: (e) => setPickupError(describeError(e).message),
  });

  const activeRun = chat.runs.find((r) => r.runId === chat.activeRunId);
  const ordersError = orders.isError ? describeError(orders.error).message : null;

  const groups: TraceGroup[] = useMemo(
    () =>
      chat.runs.map((r, i) => ({
        key: r.runId,
        title: `Run ${i + 1}: ${RUN_LABEL[r.kind]}`,
        active: r.runId === chat.activeRunId && !isTerminal(r.status),
        events: r.events,
      })),
    [chat.runs, chat.activeRunId],
  );
  const stepCount = useMemo(() => chat.runs.reduce((n, r) => n + buildTraceRows(r.events).length, 0), [chat.runs]);

  return (
    <>
      <OrderList
        orders={orders.data}
        loading={orders.isPending}
        error={ordersError}
        selectedOrderId={selectedOrderId}
        onSelect={setSelectedOrderId}
        onRetry={() => void orders.refetch()}
        onSimulateDelivery={(id) => delivery.mutate(id)}
        deliveryBusyId={deliveryBusyId}
        deliveryError={deliveryError}
      />

      <Chat
        idPrefix="aldo"
        speaker="ALDO Returns Agent"
        title="Chat with the ALDO Returns Agent"
        subtitle={selectedOrderId ? `Talking about order ${selectedOrderId}` : "Select an order so the agent knows which one you mean."}
        inputLabel="Message to the ALDO Returns Agent"
        placeholder={selectedOrderId ? `About order ${selectedOrderId}` : "Select an order, then describe what happened"}
        emptyHint="Describe your return problem in your own words, or pick a suggestion below."
        workingText="The ALDO Returns Agent is working. Its steps appear in the tool trace below."
        entries={chat.entries}
        suggestions={prompts}
        runActive={runActive && !isTerminal(activeRun?.status)}
        sending={sending}
        error={sendError}
        onSend={(text) => send(text, selectedOrderId)}
        onSuggest={onSuggest}
        onNewConversation={onRestart}
        canReset={chat.entries.length > 0 || chat.runs.length > 0}
      />

      {latestQuoteId && (
        <OptionCards
          quote={quote}
          quoteId={latestQuoteId}
          loading={quoteQuery.isPending}
          error={quoteQuery.isError ? describeError(quoteQuery.error).message : null}
          runActive={runActive}
          requoting={requote.isPending}
          onApprove={(option, trigger) => {
            triggerRef.current = trigger;
            setApproveError(null);
            setTarget({ quoteId: latestQuoteId, option });
          }}
          onRequote={requoteNow}
        />
      )}

      {latestTxnId && (
        <ConfirmationPanel
          txn={txn}
          loading={txnQuery.isPending}
          error={txnQuery.isError ? describeError(txnQuery.error).message : null}
          onSimulatePickup={(id) => pickup.mutate(id)}
          pickupBusy={pickup.isPending}
          pickupDone={pickupDone}
          pickupError={pickupError}
        />
      )}

      {latestCaseId && <CasePanel caseId={latestCaseId} view={caseView} />}

      <details className="card trace-details" open data-testid="aldo-trace-details">
        <summary>
          ALDO Returns Agent tool trace <span className="muted">({stepCount} steps)</span>
        </summary>
        <TraceTimeline
          groups={groups}
          emptyText="Steps appear here as the ALDO Returns Agent works on your message."
          ariaLabel="ALDO Returns Agent tool trace"
          testId="aldo-trace"
        />
      </details>

      {target && (
        <ApproveDialog
          quoteId={target.quoteId}
          option={target.option}
          busy={approve.isPending}
          error={approveError}
          returnFocusRef={triggerRef}
          onConfirm={() => approve.mutate(target)}
          onCancel={() => {
            setTarget(null);
            setApproveError(null);
          }}
          onRequote={requoteNow}
        />
      )}
    </>
  );
}
