import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RunEvent } from "../api/types";
import { useRunPolling } from "../hooks/useRunPolling";
import { describeError, type ErrorInfo } from "../lib/errors";
import { buildTraceRows, isTerminal } from "../lib/events";
import { formatCents, formatDate } from "../lib/format";
import { normalizeMemory, normalizePurchase } from "../lib/normalize";
import { PA_PROMPT, type SuggestedPrompt } from "../lib/prompts";
import { chatReducer, initialChat } from "../state/chat";
import { SimulatedBadge } from "./Badge";
import { Chat } from "./Chat";
import { ConfirmDialog } from "./ConfirmDialog";
import { MemoryCard } from "./MemoryCard";
import { PurchaseCard, type PlacedOrder } from "./PurchaseCard";
import { TraceTimeline, type TraceGroup } from "./TraceTimeline";

/** Whether ALDO's size update to ALDO bot is still owed (see Workspace). */
export type WritebackState = "idle" | "waiting" | "late";

interface Props {
  /** Increments whenever the A2A feed shows a memory_updated event. */
  memoryVersion: number;
  /** size_request_id of the latest size_recommended event on the A2A feed. */
  proposalId: string | undefined;
  /** Increments whenever the A2A feed shows a size_confirmed event. */
  confirmedCount: number;
  writeback: WritebackState;
  onCheckAgain: () => void;
  onActive: (active: boolean) => void;
  onRestart: () => void;
}

const PA_SUGGESTIONS = [PA_PROMPT];

/** ALDO bot, the owner's personal agent: memory card, chat, purchase proposal, and its own tool trace. */
export function PersonalAgentPanel({ memoryVersion, proposalId, confirmedCount, writeback, onCheckAgain, onActive, onRestart }: Props) {
  const qc = useQueryClient();
  const [chat, dispatch] = useReducer(chatReducer, initialChat);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<ErrorInfo | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<ErrorInfo | null>(null);
  const [placed, setPlaced] = useState<PlacedOrder | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const lastPrompt = useRef<string>(PA_PROMPT.text);

  const memoryQuery = useQuery({ queryKey: ["pa-memory"], queryFn: api.paMemory, retry: 1 });
  const memory = useMemo(() => (memoryQuery.data ? normalizeMemory(memoryQuery.data) : undefined), [memoryQuery.data]);

  const purchaseQuery = useQuery({
    queryKey: ["purchase", proposalId],
    enabled: !!proposalId,
    queryFn: () => api.purchase(proposalId as string),
    retry: 2,
  });
  const purchase = useMemo(
    () => (proposalId && purchaseQuery.data ? normalizePurchase(proposalId, purchaseQuery.data) : undefined),
    [proposalId, purchaseQuery.data],
  );

  const onEvents = useCallback(
    (runId: string, events: RunEvent[], status: string) => dispatch({ type: "events", runId, events, status }),
    [],
  );
  useRunPolling(chat.activeRunId, onEvents);

  const runActive = chat.activeRunId !== null;
  useEffect(() => {
    onActive(runActive);
  }, [runActive, onActive]);

  // A finished run may have recorded a proposal or changed memory; confirmed purchases create orders.
  const finishedRuns = chat.runs.filter((r) => isTerminal(r.status)).length;
  useEffect(() => {
    if (finishedRuns > 0) {
      void qc.invalidateQueries({ queryKey: ["purchase"] });
      void qc.invalidateQueries({ queryKey: ["pa-memory"] });
    }
  }, [finishedRuns, qc]);
  useEffect(() => {
    if (memoryVersion > 0) void qc.invalidateQueries({ queryKey: ["pa-memory"] });
  }, [memoryVersion, qc]);
  useEffect(() => {
    if (confirmedCount > 0) {
      void qc.invalidateQueries({ queryKey: ["purchase"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
    }
  }, [confirmedCount, qc]);
  useEffect(() => {
    setPlaced(null);
    setConfirmError(null);
  }, [proposalId]);

  const send = useCallback(
    async (text: string): Promise<boolean> => {
      setSending(true);
      setSendError(null);
      try {
        let convId = chat.convId;
        if (!convId) {
          const created = await api.paCreateConversation();
          convId = created.conv_id;
          dispatch({ type: "conversation", convId });
        }
        const res = await api.paSendMessage(convId, text);
        lastPrompt.current = text;
        dispatch({ type: "customer", text });
        dispatch({ type: "run_started", runId: res.run_id, kind: "personal" });
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

  const activeRun = chat.runs.find((r) => r.runId === chat.activeRunId);
  const agentBusy = runActive && !isTerminal(activeRun?.status);

  const confirm = useMutation({
    mutationFn: (v: { id: string; optionId: string }) => api.confirmPurchase(v.id, v.optionId),
    onSuccess: (res) => {
      setConfirming(false);
      setConfirmError(null);
      setPlaced({ orderId: res.order_id, nextStep: res.next_step, replayed: res.replayed });
      dispatch({ type: "action", text: `You confirmed the purchase. Simulated order ${res.order_id} placed.` });
      void qc.invalidateQueries({ queryKey: ["purchase"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
    },
    onError: (e) => {
      setConfirmError(describeError(e, "purchase"));
      void qc.invalidateQueries({ queryKey: ["purchase"] });
    },
  });

  function askAgain() {
    setConfirming(false);
    setConfirmError(null);
    void send(lastPrompt.current);
  }

  const groups: TraceGroup[] = useMemo(
    () =>
      chat.runs.map((r, i) => ({
        key: r.runId,
        title: `Run ${i + 1}: owner request`,
        active: r.runId === chat.activeRunId && !isTerminal(r.status),
        events: r.events,
      })),
    [chat.runs, chat.activeRunId],
  );
  const stepCount = useMemo(() => chat.runs.reduce((n, r) => n + buildTraceRows(r.events).length, 0), [chat.runs]);

  return (
    <>
      <MemoryCard
        memory={memory}
        loading={memoryQuery.isPending}
        error={memoryQuery.isError ? describeError(memoryQuery.error).message : null}
        writeback={writeback}
        onCheckAgain={onCheckAgain}
      />

      <Chat
        idPrefix="pa"
        speaker="ALDO bot"
        title="Chat with ALDO bot"
        subtitle="ALDO bot shops for you. It checks its memory, asks ALDO's gateway for a fit signal, and proposes a purchase. You confirm."
        inputLabel="Message to ALDO bot"
        placeholder="Ask ALDO bot to buy something"
        emptyHint="Ask ALDO bot to buy a pair, or use the suggestion below."
        workingText="ALDO bot is working. Its steps appear in the A2A trace."
        entries={chat.entries}
        suggestions={PA_SUGGESTIONS}
        runActive={agentBusy}
        sending={sending}
        error={sendError}
        onSend={send}
        onSuggest={(p: SuggestedPrompt) => void send(p.text)}
        onNewConversation={onRestart}
        canReset={chat.entries.length > 0 || chat.runs.length > 0}
      />

      {proposalId && (
        <PurchaseCard
          proposalId={proposalId}
          view={purchase}
          loading={purchaseQuery.isPending}
          error={purchaseQuery.isError ? describeError(purchaseQuery.error).message : null}
          agentBusy={agentBusy}
          placed={placed}
          onConfirm={(trigger) => {
            triggerRef.current = trigger;
            setConfirmError(null);
            setConfirming(true);
          }}
          onAskAgain={askAgain}
        />
      )}

      <details className="card trace-details" open data-testid="pa-trace-details">
        <summary>
          ALDO bot tool trace <span className="muted">({stepCount} steps, owner-facing)</span>
        </summary>
        <TraceTimeline
          groups={groups}
          emptyText="Steps appear here when ALDO bot works."
          ariaLabel="ALDO bot tool trace"
          testId="pa-trace"
        />
      </details>

      {confirming && proposalId && purchase && (
        <ConfirmDialog
          kind="purchase"
          title="Confirm your purchase"
          description="ALDO bot proposed this purchase. Check the details, then confirm. Nothing is ordered until you confirm, and this prototype only creates a simulated order."
          heading={
            <>
              {purchase.productName ?? "ALDO style"}, size {purchase.size ?? "?"} <SimulatedBadge />
            </>
          }
          facts={[
            { label: "Product", value: `${purchase.productName ?? "ALDO style"}${purchase.color ? `, ${purchase.color.toLowerCase()}` : ""}` },
            { label: "Size", value: purchase.size ?? "?" },
            { label: "Fulfilment", value: `${purchase.optionLabel ?? "Option"}${purchase.locationName ? ` at ${purchase.locationName}` : ""}` },
            { label: "ETA", value: purchase.etaDate ? formatDate(purchase.etaDate) : (purchase.etaText ?? "Not provided") },
            { label: "Price", value: formatCents(purchase.priceCents, purchase.currency) },
            ...(purchase.rationalePublic ? [{ label: "Shared reason", value: purchase.rationalePublic }] : []),
          ]}
          bullets={["Simulated order only: no payment is taken."]}
          footnote={`Request ${proposalId}${purchase.optionId ? `, option ${purchase.optionId}` : ""}`}
          confirmLabel="Confirm and place order"
          busy={confirm.isPending}
          error={confirmError}
          errorActionLabel="Ask ALDO bot again"
          onErrorAction={askAgain}
          returnFocusRef={triggerRef}
          onConfirm={() => purchase.optionId && confirm.mutate({ id: proposalId, optionId: purchase.optionId })}
          onCancel={() => {
            setConfirming(false);
            setConfirmError(null);
          }}
        />
      )}
    </>
  );
}
