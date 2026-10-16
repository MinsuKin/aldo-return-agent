import { useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { describeError } from "../../../lib/errors";
import { colorWord, formatCents, friendlyDate } from "../../../lib/format";
import { normalizeTransaction } from "../../../lib/normalize";
import { Badge } from "../../Badge";
import { Icon } from "../../Icon";
import { CardFrame, ProductPhoto, Swatch, useOrderLookup } from "./shared";

interface Props {
  transactionId: string;
  /** Ask the A2A feed to poll quickly (the pickup control adds an event to it). */
  onNudge: () => void;
}

function titleFor(kind: "exchange" | "return", optionType: string | undefined, items: number): string {
  if (kind === "exchange") return "Exchange confirmed";
  if (optionType === "return_mail") return items > 1 ? "Return label created for your bundle" : "Return label created";
  return items > 1 ? "Return confirmed for your bundle" : "Return confirmed";
}

/** The saved record of an approved exchange or return, read back from the server. */
export function TransactionCard({ transactionId, onNudge }: Props) {
  const qc = useQueryClient();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const { lookup } = useOrderLookup();
  const query = useQuery({ queryKey: ["txn", transactionId], queryFn: () => api.transaction(transactionId), retry: 3 });
  const txn = useMemo(() => (query.data ? normalizeTransaction(query.data, { lookup }) : undefined), [query.data, lookup]);

  const pickup = useMutation({
    mutationFn: () => api.simulatePickup(transactionId),
    onSuccess: () => {
      onNudge();
      void qc.invalidateQueries({ queryKey: ["pa-memory"] });
    },
  });

  const titleId = `txn-title-${transactionId}`;
  const items = txn?.lines.length ?? 0;
  const swapColor = txn?.replacementColor;

  return (
    <CardFrame
      title={txn ? titleFor(txn.kind, txn.optionType, items) : "Confirming"}
      titleId={titleId}
      headingRef={headingRef}
      testId="txn-card"
      status={txn ? "confirmed" : "loading"}
      meta={<span className="mono muted small" data-testid="txn-id">{transactionId}</span>}
    >
      {query.isPending && <p role="status">Reading the saved record...</p>}
      {query.isError && !txn && (
        <p className="alert alert-error" role="alert">
          {describeError(query.error).message}
        </p>
      )}

      {txn && (
        <>
          {items > 1 && (
            <ol className="bundle-list" data-testid="txn-items">
              {txn.lines.map((l) => (
                <li key={l.lineId}>
                  <ProductPhoto name={l.productName} color={l.color} size="row" />
                  <span className="bundle-name">
                    {l.productName}
                    {l.kind ? ` ${l.kind}` : ""}
                    <span className="muted">
                      {l.color ? `, ${colorWord(l.color)}` : ""}
                      {l.size ? `, size ${l.size}` : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}

          <dl className="facts">
            {txn.optionLabel && (
              <div>
                <dt>What</dt>
                <dd>
                  {txn.optionLabel}
                  {txn.kind === "exchange" && swapColor && txn.replacementSize ? (
                    <>
                      {" · "}
                      <Swatch color={swapColor} /> {colorWord(swapColor)} {txn.replacementSize}
                    </>
                  ) : txn.replacementSize ? (
                    ` · size ${txn.replacementSize}`
                  ) : null}
                </dd>
              </div>
            )}
            {txn.locationName && (
              <div>
                <dt>{txn.optionType === "return_mail" ? "Send to" : "Where"}</dt>
                <dd>{txn.locationName}</dd>
              </div>
            )}
            {txn.etaDate && txn.kind === "exchange" && (
              <div>
                <dt>Ready</dt>
                <dd>{friendlyDate(txn.etaDate)}</dd>
              </div>
            )}
            {txn.kind === "return" && txn.refundCents !== undefined && (
              <div>
                <dt>Refund</dt>
                <dd>
                  <strong>{formatCents(txn.refundCents, txn.currency)}</strong>
                  {txn.feeCents !== undefined && txn.optionType === "return_mail" ? ` · shipping ${formatCents(txn.feeCents, txn.currency)}` : ""}
                </dd>
              </div>
            )}
            <div>
              <dt>Next step</dt>
              <dd data-testid="txn-next-step">{txn.nextStep ?? "No further action is required."}</dd>
            </div>
          </dl>

          {txn.benefit && (
            <p className="benefit">
              <Icon name="benefit" size={14} />
              <span>{txn.benefit.note}</span>
            </p>
          )}

          {txn.storeNotice && (
            <div className="store-notice" data-testid="store-notice">
              <p className="store-notice-head">
                <Icon name="store" size={14} /> ALDO Store Desk notified
                {txn.storeNotice.storeName ? `: ${txn.storeNotice.storeName}` : ""}
                {txn.storeNotice.etaMinutes !== undefined ? <Badge tone="neutral">{txn.storeNotice.etaMinutes} min away</Badge> : null}
              </p>
              {txn.storeNotice.summary && <p className="muted">{txn.storeNotice.summary}</p>}
            </div>
          )}

          {txn.kind === "exchange" && (
            <div className="demo-control">
              <button
                type="button"
                className="btn btn-small btn-demo"
                disabled={pickup.isPending || pickup.isSuccess}
                onClick={() => pickup.mutate()}
                data-testid="simulate-pickup"
              >
                {pickup.isPending ? "Working..." : pickup.isSuccess ? "Pickup recorded" : "Simulate pickup"}
              </button>
              <span className="muted small">Demo control: the owner collects the pair and keeps it.</span>
            </div>
          )}
          {pickup.isError && (
            <p className="alert alert-error" role="alert">
              {describeError(pickup.error).message}
            </p>
          )}
        </>
      )}
    </CardFrame>
  );
}
