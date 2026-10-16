import { useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { useNow } from "../../../hooks/useNow";
import { describeError } from "../../../lib/errors";
import { colorWord, formatCents, friendlyDate } from "../../../lib/format";
import { credibleProvenance, normalizePurchase, type PurchaseView } from "../../../lib/normalize";
import { Badge, ProvenanceBadge } from "../../Badge";
import { Icon } from "../../Icon";
import { CardFrame, Countdown, ProductPhoto, useFocusWhenReady } from "./shared";

export interface ReviewEvidence {
  count: number;
  flagged: number;
}

interface Props {
  sizeRequestId: string;
  /** The newest proposal in the chat; older ones are shown as replaced. */
  isLatest: boolean;
  /** The personal agent is still working, so the proposal may still change. */
  busy: boolean;
  /** From the A2A stream (reviews_shared): counts only, never review text. */
  reviews?: ReviewEvidence;
  onConfirm: (view: PurchaseView, trigger: HTMLElement) => void;
  onAskAgain: () => void;
}

/** The personal agent's purchase proposal, built from the server's record. Only the owner's click places the order. */
export function PurchaseCard({ sizeRequestId, isLatest, busy, reviews, onConfirm, onAskAgain }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const query = useQuery({ queryKey: ["purchase", sizeRequestId], queryFn: () => api.purchase(sizeRequestId), retry: 2 });
  const view = useMemo(() => (query.data ? normalizePurchase(sizeRequestId, query.data) : undefined), [query.data, sizeRequestId]);

  const proposed = view?.status === "proposed";
  const decided = view?.status === "decided";
  const now = useNow(proposed);
  const remaining = view?.expiresAt ? view.expiresAt - now : null;
  const expired = !!view && !decided && (view.status === "expired" || (proposed && remaining !== null && remaining <= 0));
  const replaced = !!view && !decided && !isLatest;
  const ready = proposed && !busy && isLatest && !expired;
  useFocusWhenReady(headingRef, ready);

  const titleId = `purchase-title-${sizeRequestId}`;
  const title = decided ? "Purchase confirmed" : replaced ? "Earlier proposal" : "Purchase proposal";
  const productLine = view ? `${view.productName ?? "ALDO style"}${view.color ? `, ${colorWord(view.color)}` : ""}` : "";

  return (
    <CardFrame
      title={title}
      titleId={titleId}
      headingRef={headingRef}
      testId="purchase-card"
      status={decided ? "decided" : replaced ? "replaced" : expired ? "expired" : (view?.status ?? "loading")}
    >
      {query.isPending && <p role="status">Loading the proposal...</p>}
      {query.isError && !view && (
        <p className="alert alert-error" role="alert">
          {describeError(query.error).message}
        </p>
      )}

      {view && view.status === "open" && <p className="muted">Your personal agent is still preparing a proposal. Nothing has been ordered.</p>}

      {view && (proposed || decided || expired) && (
        <>
          {decided ? (
            <p className="muted" data-testid="purchase-confirmed-line">
              {productLine}, size {view.size ?? "?"}. The order is below.
            </p>
          ) : (
            <div className="product-row">
              <ProductPhoto name={view.productName} color={view.color} size="hero" />
              <div>
                <p className="product-name">{productLine}</p>
                <p className="product-size">
                  Size <strong>{view.size ?? "?"}</strong>
                </p>
              </div>
            </div>
          )}

          {replaced ? (
            <p className="muted">Replaced by a newer proposal below.</p>
          ) : decided ? null : (
            <>
              <ul className="md-list" data-testid="purchase-facts">
                <li>
                  <strong>Method:</strong> {view.optionLabel ?? "Option"}
                  {view.locationName ? ` · ${view.locationName}` : ""}
                </li>
                <li>
                  <strong>{view.optionType === "store_pickup" ? "Ready" : "Arrives"}:</strong>{" "}
                  {view.etaDate ? friendlyDate(view.etaDate) : (view.etaText ?? "Not provided")}{" "}
                  {view.meetsDeadline === true && (
                    <Badge tone="neutral">
                      <Icon name="check" size={12} /> Meets your deadline
                    </Badge>
                  )}
                  {view.meetsDeadline === false && <Badge tone="neutral">After your deadline</Badge>}
                </li>
                <li>
                  <strong>Price:</strong> {formatCents(view.priceCents, view.currency)}
                </li>
              </ul>

              {proposed && (
                <div className="md-section">
                  <p className="md-heading">Why this size</p>
                  <ul className="md-list" aria-label="Why this size">
                    {view.rationalePublic && (
                      <li data-testid="purchase-rationale">
                        {view.rationalePublic}
                        {view.confidence ? <span className="muted"> ({view.confidence} confidence)</span> : null}
                      </li>
                    )}
                    {view.fitHeadline && (
                      <li>
                        {view.fitHeadline}{" "}
                        {credibleProvenance(view.fitProvenance).map((p) => (
                          <ProvenanceBadge key={p} kind={p} />
                        ))}
                      </li>
                    )}
                    {reviews && (
                      <li data-testid="reviews-evidence">
                        Read {reviews.count} reviews
                        {reviews.flagged > 0 ? `, ${reviews.flagged} flagged and excluded` : ""}
                      </li>
                    )}
                    <li>Your own size history stays with your personal agent.</li>
                  </ul>
                </div>
              )}

              {proposed && !expired && <Countdown expiresAt={view.expiresAt} active label="Expires" testId="purchase-countdown" />}

              {expired && (
                <div className="alert alert-warn" role="status" data-testid="purchase-expired">
                  <p>This proposal expired, so it can no longer be confirmed. Nothing was ordered.</p>
                  <button type="button" className="btn btn-small" onClick={onAskAgain} disabled={busy}>
                    Ask your personal agent again
                  </button>
                </div>
              )}

              {!expired && (
                <div className="card-actions">
                  <button
                    type="button"
                    className="btn btn-primary btn-block"
                    disabled={busy || !proposed || !isLatest}
                    onClick={(e) => onConfirm(view, e.currentTarget)}
                    data-testid="confirm-purchase-open"
                  >
                    Confirm purchase
                  </button>
                  <p className="muted small">{busy ? "Available when your personal agent finishes." : "Nothing is ordered until you confirm."}</p>
                </div>
              )}
            </>
          )}
        </>
      )}
    </CardFrame>
  );
}
