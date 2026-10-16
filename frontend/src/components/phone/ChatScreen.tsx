import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { RunEvent } from "../../api/types";
import type { Conversation } from "../../hooks/useConversation";
import type { WritebackState } from "../../hooks/useWriteback";
import { describeError, type ErrorInfo } from "../../lib/errors";
import { colorWord, formatCents, friendlyDate } from "../../lib/format";
import { normalizeMemory, type OptionView, type PurchaseView, type QuoteView } from "../../lib/normalize";
import { quickRepliesFor, type QuickReply } from "../../lib/script";
import { num, pick } from "../../lib/util";
import type { CardRef } from "../../state/conversation";
import { Icon } from "../Icon";
import { BotAvatar } from "./Avatars";
import { CaseCard } from "./cards/CaseCard";
import { OrderCard } from "./cards/OrderCard";
import { PurchaseCard, type ReviewEvidence } from "./cards/PurchaseCard";
import { QuoteCard, confirmLabel } from "./cards/QuoteCard";
import { TransactionCard } from "./cards/TransactionCard";
import { Composer } from "./Composer";
import { ConfirmSheet, type SheetFact, type SheetItem } from "./ConfirmSheet";
import { MemorySheet } from "./MemorySheet";
import { MessageList } from "./MessageList";
import { PushBanner } from "./PushBanner";
import { QuickReplies } from "./QuickReplies";
import { ReasonChips, isAskingForReason } from "./ReasonChips";

export interface ChatCustomer {
  customerId: string;
  name: string;
  tier: string;
  tierLabel?: string;
  locationLabel?: string;
}

interface Props {
  customer: ChatCustomer;
  convo: Conversation;
  /** The shared A2A stream (reviews evidence and fallbacks come from it). */
  a2a: RunEvent[];
  writeback: WritebackState;
  onCheckAgain: () => void;
  /** Ask the A2A feed to poll quickly. */
  onNudge: () => void;
  onSwitch: () => void;
  /** Tells the stage that a sheet is open, so the trace can be inert too. */
  onModalChange: (open: boolean) => void;
}

type Sheet = { kind: "purchase"; view: PurchaseView } | { kind: "approve"; quote: QuoteView; option: OptionView } | { kind: "memory" };

function purchaseFacts(v: PurchaseView): SheetFact[] {
  return [
    { label: "Product", value: `${v.productName ?? "ALDO style"}${v.color ? `, ${colorWord(v.color)}` : ""}` },
    { label: "Size", value: v.size ?? "?" },
    { label: "Delivery", value: `${v.optionLabel ?? "Option"}${v.locationName ? ` · ${v.locationName}` : ""}` },
    { label: v.optionType === "store_pickup" ? "Ready" : "Arrives", value: v.etaDate ? friendlyDate(v.etaDate) : (v.etaText ?? "Not provided") },
    { label: "Price", value: formatCents(v.priceCents, v.currency) },
    ...(v.rationalePublic ? [{ label: "Why this size", value: v.rationalePublic }] : []),
  ];
}

function approveContent(o: OptionView): { facts: SheetFact[]; items?: SheetItem[] } {
  const bundle = o.lines.length > 1;
  const facts: SheetFact[] = [];
  if (o.isExchange) {
    const toSize = o.replacementSize ?? o.fromSize ?? "?";
    facts.push({
      label: "Swap",
      value: o.isColorSwap
        ? `${colorWord(o.fromColor)} ${o.fromSize ?? ""} to ${colorWord(o.toColor)} ${toSize}`.replace(/\s+/g, " ").trim()
        : `Size ${o.fromSize ?? "?"} to size ${toSize}`,
    });
  }
  if (o.locationName) {
    facts.push({
      label: o.type === "return_mail" ? "Send to" : o.type.startsWith("exchange_ship") ? "Ships from" : "Where",
      value: `${o.locationName}${o.minutesAway !== undefined ? ` (${o.minutesAway} min away)` : ""}`,
    });
  }
  if (o.etaDate || o.etaText) {
    facts.push({
      label: o.type === "exchange_store_pickup" ? "Ready" : o.type.startsWith("exchange") ? "Arrives" : o.type === "return_mail" ? "Refund expected" : "When",
      value: o.etaDate ? friendlyDate(o.etaDate) : (o.etaText ?? ""),
    });
  }
  if (o.isExchange) {
    facts.push({ label: "Fee", value: o.feeCents === 0 ? "Free" : formatCents(o.feeCents, o.currency) });
  } else {
    if (bundle || o.type === "return_mail" || o.feeCents > 0) facts.push({ label: "Shipping", value: formatCents(o.feeCents, o.currency) });
    facts.push({ label: "Refund total", value: formatCents(o.refundCents, o.currency) });
  }
  if (o.benefit) facts.push({ label: "Perk", value: o.benefit.note });
  const items: SheetItem[] | undefined = bundle
    ? o.lines.map((l) => ({
        name: `${l.productName}${l.kind ? ` ${l.kind}` : ""}`,
        detail: `${l.color ? `, ${colorWord(l.color)}` : ""}${l.size ? `, size ${l.size}` : ""}`,
        price: l.refundCents !== undefined ? formatCents(l.refundCents, o.currency) : undefined,
      }))
    : undefined;
  return { facts, items };
}

/** The chat with ALDO bot: header, transcript with inline cards, quick replies, composer, sheets and push banner. */
export function ChatScreen({ customer, convo, a2a, writeback, onCheckAgain, onNudge, onSwitch, onModalChange }: Props) {
  const qc = useQueryClient();
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [sheetBusy, setSheetBusy] = useState(false);
  const [sheetError, setSheetError] = useState<ErrorInfo | null>(null);
  const [seenUpdates, setSeenUpdates] = useState(0);
  const [typeOwnReason, setTypeOwnReason] = useState(false);
  const [followKey, setFollowKey] = useState(0);
  const bumpFollow = useCallback(() => setFollowKey((k) => k + 1), []);
  const triggerRef = useRef<HTMLElement | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const refocusComposer = useRef(false);

  const memoryQuery = useQuery({ queryKey: ["pa-memory"], queryFn: api.paMemory, retry: 1 });
  const memory = useMemo(() => (memoryQuery.data ? normalizeMemory(memoryQuery.data) : undefined), [memoryQuery.data]);
  const updateCount = memory?.updates.length ?? 0;

  useEffect(() => {
    onModalChange(sheet !== null);
  }, [sheet, onModalChange]);
  useEffect(() => () => onModalChange(false), [onModalChange]);

  // After a confirmation closes its sheet the opener is gone (disabled), so land on the message box.
  useEffect(() => {
    if (sheet === null && refocusComposer.current) {
      refocusComposer.current = false;
      inputRef.current?.focus();
    }
  }, [sheet]);

  const reviews: ReviewEvidence | undefined = useMemo(() => {
    const last = [...a2a].reverse().find((e) => e.type === "reviews_shared");
    if (!last) return undefined;
    const count = num(pick(last.payload, ["n_reviews", "count", "total", "reviews_count"]));
    const flagged = num(pick(last.payload, ["n_flagged", "flagged", "flagged_count"])) ?? 0;
    return count === undefined ? undefined : { count, flagged };
  }, [a2a]);

  const askAgain = useCallback(() => {
    setSheet(null);
    setSheetError(null);
    const text = convo.lastUserText();
    if (text) void convo.send(text).then((ok) => ok && bumpFollow());
  }, [convo, bumpFollow]);

  const openMemory = (e: { currentTarget: HTMLElement }) => {
    triggerRef.current = e.currentTarget;
    setSeenUpdates(updateCount);
    setSheet({ kind: "memory" });
  };

  const confirmPurchase = async () => {
    if (sheet?.kind !== "purchase") return;
    setSheetBusy(true);
    setSheetError(null);
    try {
      await convo.confirmPurchase(sheet.view);
      refocusComposer.current = true;
      bumpFollow();
      setSheet(null);
    } catch (e) {
      setSheetError(describeError(e, "purchase"));
      void qc.invalidateQueries({ queryKey: ["purchase"] });
    } finally {
      setSheetBusy(false);
    }
  };

  const confirmOption = async () => {
    if (sheet?.kind !== "approve") return;
    setSheetBusy(true);
    setSheetError(null);
    try {
      await convo.approve(sheet.quote.quoteId, sheet.option);
      refocusComposer.current = true;
      bumpFollow();
      setSheet(null);
    } catch (e) {
      setSheetError(describeError(e));
      void qc.invalidateQueries({ queryKey: ["quote"] });
    } finally {
      setSheetBusy(false);
    }
  };

  const requote = (quote: QuoteView) => {
    setSheet(null);
    setSheetError(null);
    void convo.requote(quote).catch((e: unknown) => setSheetError(describeError(e)));
  };

  const renderCard = (card: CardRef, isLatest: boolean): ReactNode => {
    switch (card.type) {
      case "purchase":
        return (
          <PurchaseCard
            sizeRequestId={card.sizeRequestId}
            isLatest={isLatest}
            busy={convo.busy}
            reviews={reviews}
            onConfirm={(view, trigger) => {
              triggerRef.current = trigger;
              setSheetError(null);
              setSheet({ kind: "purchase", view });
            }}
            onAskAgain={askAgain}
          />
        );
      case "quote":
        return (
          <QuoteCard
            quoteId={card.quoteId}
            isLatest={isLatest}
            busy={convo.busy}
            onChoose={(quote, option, trigger) => {
              triggerRef.current = trigger;
              setSheetError(null);
              setSheet({ kind: "approve", quote, option });
            }}
            onRequote={requote}
          />
        );
      case "order":
        return <OrderCard orderId={card.orderId} sizeRequestId={card.sizeRequestId} delivery={convo.deliveries[card.orderId]} onRetryDelivery={convo.retryDelivery} />;
      case "txn":
        return <TransactionCard transactionId={card.transactionId} onNudge={onNudge} />;
      case "case":
        return <CaseCard caseId={card.caseId} />;
    }
  };

  const replies = quickRepliesFor(customer.customerId, {
    userTexts: convo.userTexts,
    delivered: convo.delivered,
    confirmedCount: convo.confirmedCount,
  });
  const send = useCallback(
    async (text: string): Promise<boolean> => {
      const ok = await convo.send(text);
      if (ok) bumpFollow();
      return ok;
    },
    [convo, bumpFollow],
  );
  const pick1 = (r: QuickReply) => {
    void send(r.text).then((ok) => {
      if (ok) inputRef.current?.focus();
    });
  };
  // While ALDO bot waits for the reason of a return, offer the usual reasons as one-tap answers.
  const askingReason = isAskingForReason(convo.entries);
  useEffect(() => {
    if (!askingReason) setTypeOwnReason(false);
  }, [askingReason]);
  const pickReason = (text: string) => {
    void send(text).then((ok) => {
      if (ok) inputRef.current?.focus();
    });
  };

  const typing = convo.busy ? (convo.activeAgent === "returns" ? "ALDO bot is working on your return" : "ALDO bot is typing") : null;
  const greeting = `Hi ${customer.name}. I'm ALDO bot. I can buy shoes for you, remember your sizes, and sort out exchanges and returns. Nothing is ordered until you confirm it.`;
  const returning = convo.mode === "returns";

  return (
    <div className="screen chat" data-testid="chat-screen">
      <header className="chat-header">
        <button type="button" className="back-btn" onClick={onSwitch} aria-label="Switch customer" data-testid="switch-customer">
          <Icon name="back" size={20} />
          <span>Switch</span>
        </button>
        <div className="chat-title">
          <BotAvatar size={40} />
          <div className="chat-title-text">
            <h2>ALDO bot</h2>
            <p className="chat-sub" data-testid="chat-sub">
              {customer.name}
              {customer.tierLabel ? ` · ${customer.tierLabel}` : ""}
            </p>
          </div>
        </div>
        <div className="chat-tools">
          <span className="workflow-pill" data-mode={returning ? "returns" : "bot"} data-testid="workflow-pill">
            <span className="sr-only">Workflow: </span>
            {returning ? "Return" : "Purchase"}
          </span>
          <button type="button" className="icon-btn memory-btn" onClick={openMemory} aria-label="What ALDO bot remembers" title="What ALDO bot remembers" data-testid="memory-open">
            <Icon name="memory" size={20} />
            {updateCount > seenUpdates && <span className="memory-dot" data-testid="memory-dot" aria-hidden="true" />}
          </button>
        </div>
      </header>

      <MessageList entries={convo.entries} greeting={greeting} typing={typing} renderCard={renderCard} followKey={followKey} />
      {askingReason && (
        <ReasonChips
          disabled={convo.busy}
          onPick={pickReason}
          onOther={() => {
            setTypeOwnReason(true);
            inputRef.current?.focus();
          }}
        />
      )}
      <QuickReplies replies={replies} disabled={convo.busy} onPick={pick1} />
      <Composer inputRef={inputRef} busy={convo.busy} error={convo.sendError} placeholder={askingReason && typeOwnReason ? "Type your reason" : undefined} onSend={send} />
      <PushBanner note={convo.push} onDismiss={convo.dismissPush} />

      {sheet?.kind === "purchase" && (
        <ConfirmSheet
          kind="purchase"
          title="Confirm your purchase"
          description="ALDO bot proposed this purchase. Check the details, then confirm. Nothing is ordered until you confirm."
          heading={`${sheet.view.productName ?? "ALDO style"}, size ${sheet.view.size ?? "?"}`}
          facts={purchaseFacts(sheet.view)}
          footnote={`Request ${sheet.view.sizeRequestId}${sheet.view.optionId ? `, option ${sheet.view.optionId}` : ""}`}
          confirmLabel="Confirm and place order"
          busy={sheetBusy}
          error={sheetError}
          errorActionLabel="Ask ALDO bot again"
          onErrorAction={askAgain}
          returnFocusRef={triggerRef}
          onConfirm={() => void confirmPurchase()}
          onCancel={() => {
            setSheet(null);
            setSheetError(null);
          }}
        />
      )}

      {sheet?.kind === "approve" && (
        <ConfirmSheet
          kind="approve"
          title={sheet.option.isExchange ? "Confirm your exchange" : "Confirm your return"}
          description="Check the details, then confirm. Nothing happens until you confirm."
          heading={sheet.option.typeLabel}
          {...approveContent(sheet.option)}
          bullets={sheet.option.conditions}
          footnote={`Quote ${sheet.quote.quoteId}, option ${sheet.option.optionId}${sheet.option.payloadHash ? `, fingerprint ${sheet.option.payloadHash.slice(0, 12)}` : ""}`}
          confirmLabel={confirmLabel(sheet.option)}
          busy={sheetBusy}
          error={sheetError}
          errorActionLabel="Ask for new options"
          onErrorAction={() => requote(sheet.quote)}
          returnFocusRef={triggerRef}
          onConfirm={() => void confirmOption()}
          onCancel={() => {
            setSheet(null);
            setSheetError(null);
          }}
        />
      )}

      {sheet?.kind === "memory" && (
        <MemorySheet
          memory={memory}
          loading={memoryQuery.isPending}
          error={memoryQuery.isError ? describeError(memoryQuery.error).message : null}
          writeback={writeback}
          onCheckAgain={onCheckAgain}
          returnFocusRef={triggerRef}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
