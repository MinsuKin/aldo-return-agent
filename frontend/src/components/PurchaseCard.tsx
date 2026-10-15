import { useEffect, useRef } from "react";
import { useNow } from "../hooks/useNow";
import { formatCents, formatCountdown, formatDate } from "../lib/format";
import type { PurchaseView } from "../lib/normalize";
import { Badge, ProvenanceBadge, SimulatedBadge } from "./Badge";
import { Icon } from "./Icon";

export interface PlacedOrder {
  orderId: string;
  nextStep?: string;
  replayed?: boolean;
}

interface Props {
  proposalId: string;
  view: PurchaseView | undefined;
  loading: boolean;
  error: string | null;
  /** ALDO bot is still working, so the proposal may still change. */
  agentBusy: boolean;
  /** Result of this browser's confirm call (the server view also carries it once decided). */
  placed: PlacedOrder | null;
  onConfirm: (trigger: HTMLElement) => void;
  onAskAgain: () => void;
}

/** ALDO bot's purchase proposal, rendered from the server's record. Only the owner's click places the order. */
export function PurchaseCard({ proposalId, view, loading, error, agentBusy, placed, onConfirm, onAskAgain }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const proposed = view?.status === "proposed";
  const decided = view?.status === "decided" || !!placed;
  const now = useNow(proposed && !decided);
  const remaining = view?.expiresAt ? view.expiresAt - now : null;
  const expired = !decided && !!view && (view.status === "expired" || (proposed && remaining !== null && remaining <= 0));

  // Bring the proposal into view once ALDO bot has finished, and move focus to it for keyboard users.
  const ready = !!view && proposed && !agentBusy;
  useEffect(() => {
    if (!ready) return;
    const el = headingRef.current;
    el?.scrollIntoView({ block: "nearest" });
    el?.focus({ preventScroll: true });
  }, [ready, proposalId]);

  // Focus the result when the order is placed.
  const placedKey = placed?.orderId;
  useEffect(() => {
    if (placedKey) headingRef.current?.focus();
  }, [placedKey]);

  const orderId = placed?.orderId ?? view?.orderId;
  const nextStep = placed?.nextStep ?? view?.nextStep;

  return (
    <section className={`card purchase${decided ? " panel-success" : ""}`} aria-labelledby="purchase-card-title" data-testid="purchase-card" data-status={decided ? "decided" : (view?.status ?? "loading")}>
      <div className="card-head">
        <h3 id="purchase-card-title" ref={headingRef} tabIndex={-1}>
          {decided ? "Simulated order placed" : "Purchase proposal from ALDO bot"}
        </h3>
        <SimulatedBadge />
      </div>

      {loading && !view && <p role="status">Loading the proposal from ALDO&apos;s server...</p>}
      {error && !view && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}

      {view && view.status === "open" && !decided && (
        <p role="status" className="muted">
          ALDO bot is still preparing a proposal. Nothing has been ordered.
        </p>
      )}

      {view && (proposed || decided || expired) && (
        <>
          {!decided && (
            <p className="muted card-sub">
              ALDO bot only proposes. Nothing is ordered until you confirm. These details come from ALDO&apos;s server, not from the chat text.
            </p>
          )}
          <dl className="facts facts-wide" data-testid="purchase-facts">
            <div>
              <dt>Product</dt>
              <dd>
                {view.productName ?? "ALDO style"}
                {view.color ? `, ${view.color.toLowerCase()}` : ""}
              </dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{view.size ?? "?"}</dd>
            </div>
            <div>
              <dt>Fulfilment</dt>
              <dd>
                {view.optionLabel ?? "Option"}
                {view.locationName ? ` at ${view.locationName}` : ""}
              </dd>
            </div>
            <div>
              <dt>ETA</dt>
              <dd>
                {view.etaDate ? formatDate(view.etaDate) : (view.etaText ?? "Not provided")}{" "}
                {view.meetsDeadline === true && (
                  <Badge tone="success">
                    <Icon name="check" size={14} /> Meets your deadline
                  </Badge>
                )}
                {view.meetsDeadline === false && <Badge tone="neutral">After your deadline</Badge>}
              </dd>
            </div>
            <div>
              <dt>Price</dt>
              <dd>{formatCents(view.priceCents, view.currency)}</dd>
            </div>
            {view.fitHeadline && (
              <div>
                <dt>Fit signal</dt>
                <dd>
                  {view.fitHeadline}{" "}
                  {view.fitProvenance.map((p) => (
                    <ProvenanceBadge key={p} kind={p} />
                  ))}
                </dd>
              </div>
            )}
            {view.rationalePublic && (
              <div>
                <dt>Shared reason</dt>
                <dd data-testid="purchase-rationale">
                  {view.rationalePublic}
                  {view.confidence ? <span className="muted"> ({view.confidence} confidence)</span> : null}
                </dd>
              </div>
            )}
            {decided && orderId && (
              <div>
                <dt>Order</dt>
                <dd className="mono strong" data-testid="purchase-order-id">
                  {orderId}
                </dd>
              </div>
            )}
            {decided && (
              <div>
                <dt>Next step</dt>
                <dd data-testid="purchase-next-step">{nextStep ?? "Your order is recorded."}</dd>
              </div>
            )}
          </dl>

          {proposed && !decided && !expired && remaining !== null && remaining > 0 && (
            <p className="muted options-meta">
              <span className="countdown" data-testid="purchase-countdown">
                <Icon name="clock" size={14} /> Proposal expires in {formatCountdown(remaining)}
              </span>
            </p>
          )}

          {expired && (
            <div className="alert alert-warn" role="status" data-testid="purchase-expired">
              <p>This proposal expired, so it can no longer be confirmed. Nothing was ordered.</p>
              <button type="button" className="btn" onClick={onAskAgain} disabled={agentBusy}>
                Ask ALDO bot again
              </button>
            </div>
          )}

          {!decided && !expired && (
            <div className="option-actions">
              <button
                type="button"
                className="btn btn-primary"
                disabled={agentBusy || !proposed}
                onClick={(e) => onConfirm(e.currentTarget)}
                data-testid="confirm-purchase-open"
              >
                Confirm purchase
              </button>
              {agentBusy && <span className="muted">Available when ALDO bot finishes.</span>}
            </div>
          )}

          {decided && (
            <p className="note">
              Prototype only: this is a simulated order, no payment was taken. Next, use Simulate delivery on this order in the ALDO
              Returns Agent panel to try a return.
            </p>
          )}
        </>
      )}
    </section>
  );
}
