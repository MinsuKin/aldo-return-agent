import { useId, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { useNow } from "../../../hooks/useNow";
import { describeError } from "../../../lib/errors";
import { colorWord, formatCents, friendlyDate } from "../../../lib/format";
import { normalizeQuote, type OptionView, type QuoteView } from "../../../lib/normalize";
import { Badge } from "../../Badge";
import { Icon } from "../../Icon";
import { CardFrame, Countdown, ProductPhoto, useFocusWhenReady, useOrderLookup } from "./shared";

interface Props {
  quoteId: string;
  /** The newest set of options in the chat; older ones are shown as replaced. */
  isLatest: boolean;
  /** The personal agent is still working, so the options may still change. */
  busy: boolean;
  onChoose: (quote: QuoteView, option: OptionView, trigger: HTMLElement) => void;
  onRequote: (quote: QuoteView) => void;
}

export function confirmLabel(o: OptionView): string {
  if (o.lines.length > 1) return o.isExchange ? "Confirm bundle exchange" : "Confirm bundle return";
  return o.isExchange ? "Confirm exchange" : "Confirm return";
}

function whenLabel(o: OptionView): string {
  switch (o.type) {
    case "exchange_store_pickup":
      return "Ready";
    case "exchange_ship_from_dc":
    case "exchange_ship_from_store":
      return "Arrives";
    case "return_mail":
      return "Refund expected";
    default:
      return "When";
  }
}

function whereLabel(o: OptionView): string {
  switch (o.type) {
    case "exchange_ship_from_dc":
    case "exchange_ship_from_store":
      return "Ships from";
    case "return_mail":
      return "Send to";
    default:
      return "Where";
  }
}

/** The "what changes" line: a colour swap, a size swap, or the refund. */
function Summary({ o }: { o: OptionView }) {
  // A return names what is going back: one item, or several in one shipment.
  if (!o.isExchange && o.lines.length >= 1) {
    return (
      <div className="bundle" data-testid="bundle-items">
        <p className="bundle-count">{o.lines.length > 1 ? `${o.lines.length} items, one shipment` : "1 item"}</p>
        <ol className="bundle-list">
          {o.lines.map((l) => (
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
              {l.refundCents !== undefined && <span className="bundle-price">{formatCents(l.refundCents, o.currency)}</span>}
            </li>
          ))}
        </ol>
      </div>
    );
  }
  if (o.isExchange) {
    const toSize = o.replacementSize ?? o.fromSize;
    return (
      <div className="swap-wrap">
        {o.productName && <p className="swap-product">{o.productName}</p>}
        {o.isColorSwap ? (
          <p className="swap" data-testid="swap-summary">
            <ProductPhoto name={o.productName} color={o.fromColor} size="mini" />
            <span>
              {colorWord(o.fromColor)} {o.fromSize}
            </span>
            <Icon name="chevron-right" size={16} />
            <span className="sr-only">to</span>
            <ProductPhoto name={o.productName} color={o.toColor} size="mini" />
            <strong>
              {colorWord(o.toColor)} {toSize}
            </strong>
          </p>
        ) : (
          <p className="swap" data-testid="swap-summary">
            <ProductPhoto name={o.productName} color={o.toColor} size="mini" />
            <span>{o.fromSize ? `Size ${o.fromSize}` : "Current size"}</span>
            <Icon name="chevron-right" size={16} />
            <span className="sr-only">to</span>
            <strong>Size {toSize}</strong>
          </p>
        )}
      </div>
    );
  }
  if (o.productName) {
    return (
      <p className="swap-product">
        {o.productName}
        {o.color ? `, ${colorWord(o.color)}` : ""}
      </p>
    );
  }
  return null;
}

function Totals({ o }: { o: OptionView }) {
  if (o.isExchange) return null;
  const bundle = o.lines.length > 1;
  const gross = o.refundCents + o.feeCents;
  const showFee = bundle || o.type === "return_mail" || o.feeCents > 0;
  return (
    <dl className="totals" data-testid="option-totals">
      {bundle && (
        <div>
          <dt>Items</dt>
          <dd>{formatCents(gross, o.currency)}</dd>
        </div>
      )}
      {showFee && (
        <div>
          <dt>{bundle || o.type === "return_mail" ? "Shipping" : "Fee"}</dt>
          <dd data-testid="option-fee">
            {o.feeCents === 0 ? (
              <>
                {formatCents(0, o.currency)}
                {o.benefit?.savedCents ? (
                  <span className="strike muted">
                    {" "}
                    <span className="sr-only">instead of </span>
                    <s>{formatCents(o.benefit.savedCents, o.currency)}</s>
                  </span>
                ) : null}
              </>
            ) : (
              formatCents(o.feeCents, o.currency)
            )}
          </dd>
        </div>
      )}
      <div className="totals-main">
        <dt>Refund total</dt>
        <dd data-testid="option-refund">{formatCents(o.refundCents, o.currency)}</dd>
      </div>
    </dl>
  );
}

interface BlockProps {
  quote: QuoteView;
  option: OptionView;
  primary: boolean;
  disabled: boolean;
  approved: boolean;
  onChoose: (quote: QuoteView, option: OptionView, trigger: HTMLElement) => void;
}

function OptionBlock({ quote, option: o, primary, disabled, approved, onChoose }: BlockProps) {
  const headingId = useId();
  return (
    <article
      className={`opt${primary ? " opt-primary" : ""}${approved ? " opt-approved" : ""}`}
      aria-labelledby={headingId}
      data-testid={primary ? "primary-option" : `option-${o.optionId}`}
      data-option-type={o.type}
    >
      <header className="opt-head">
        <h4 id={headingId}>{o.typeLabel}</h4>
        {approved ? (
          <Badge tone="success">Confirmed</Badge>
        ) : o.isExchange && o.feeCents === 0 && o.lines.length <= 1 ? (
          <Badge tone="success">Free</Badge>
        ) : null}
      </header>

      <Summary o={o} />

      <dl className="facts">
        {o.locationName && (
          <div>
            <dt>{whereLabel(o)}</dt>
            <dd>
              {o.locationName}
              {o.minutesAway !== undefined && (
                <>
                  {" "}
                  <Badge tone="neutral">{o.minutesAway} min away</Badge>
                </>
              )}
              {o.locationAddress && (
                <span className="muted" style={{ display: "block", fontSize: "0.85em" }} data-testid="store-address">
                  {o.locationAddress}
                </span>
              )}
            </dd>
          </div>
        )}
        {(o.etaDate || o.etaText) && (
          <div>
            <dt>{whenLabel(o)}</dt>
            <dd>
              {o.etaDate ? friendlyDate(o.etaDate) : o.etaText}{" "}
              {o.meetsDeadline === true && (
                <Badge tone="success">
                  <Icon name="check" size={12} /> Meets your deadline
                </Badge>
              )}
              {o.meetsDeadline === false && <Badge tone="neutral">After your deadline</Badge>}
            </dd>
          </div>
        )}
        {o.isExchange && (
          <div>
            <dt>Fee</dt>
            <dd>{o.feeCents === 0 ? "Free" : formatCents(o.feeCents, o.currency)}</dd>
          </div>
        )}
      </dl>

      <Totals o={o} />

      {o.benefit && (
        <p className="benefit" data-testid="benefit-note">
          <Icon name="benefit" size={14} />
          <span>
            {o.benefit.note}
            {o.benefit.savedCents ? ` · you save ${formatCents(o.benefit.savedCents, o.currency)}` : ""}
          </span>
        </p>
      )}

      {primary && o.conditions.length > 0 && (
        <ul className="conditions">
          {o.conditions.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      {o.rankingNote && <p className="muted small">{o.rankingNote}</p>}

      {!approved && (
        <div className="card-actions">
          <button
            type="button"
            className={`btn btn-block ${primary ? "btn-primary" : ""}`}
            disabled={disabled}
            onClick={(e) => onChoose(quote, o, e.currentTarget)}
            data-testid={primary ? "confirm-option-open" : `choose-option-${o.optionId}`}
            aria-label={primary ? undefined : `Choose this instead: ${o.typeLabel}${o.locationName ? ` at ${o.locationName}` : ""}`}
          >
            {primary ? confirmLabel(o) : "Choose this instead"}
          </button>
        </div>
      )}
    </article>
  );
}

/**
 * The options for a return or exchange, built from the server's quote. The first option is the primary
 * one; the others (refund options always among them) sit under "See other options". Choosing an
 * option opens the confirmation sheet; nothing happens until the owner confirms there.
 */
export function QuoteCard({ quoteId, isLatest, busy, onChoose, onRequote }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [showOthers, setShowOthers] = useState(false);
  const panelId = useId();
  const query = useQuery({ queryKey: ["quote", quoteId], queryFn: () => api.quote(quoteId), retry: 2 });
  const { lookup } = useOrderLookup();

  const quote = useMemo(() => {
    if (!query.data) return undefined;
    const own = typeof query.data.line_id === "string" ? lookup(query.data.line_id) : undefined;
    return normalizeQuote(query.data, { productName: own?.productName, color: own?.color, size: own?.size, lookup });
  }, [query.data, lookup]);

  const open = quote?.status === "open";
  const now = useNow(!!quote && open);
  const remaining = quote?.expiresAt ? quote.expiresAt - now : null;
  const expired = !!quote && (quote.status === "expired" || (open && remaining !== null && remaining <= 0));
  const approvedId = quote?.approvedOptionId;
  const executed = quote?.status === "executed";
  const locked = !!approvedId || executed;
  const dead = !!quote && (quote.status === "superseded" || (!isLatest && !locked));
  const ready = !!quote && open && !busy && isLatest && !expired;
  useFocusWhenReady(headingRef, ready);

  const [primary, ...others] = quote?.options ?? [];
  const bundle = (primary?.lines.length ?? 0) > 1;
  const disabled = busy || expired || locked || dead;
  const titleId = `quote-title-${quoteId}`;

  return (
    <CardFrame
      title={bundle ? "Your return bundle" : "Your options"}
      titleId={titleId}
      headingRef={headingRef}
      testId="quote-card"
      status={locked ? "confirmed" : dead ? "replaced" : expired ? "expired" : (quote?.status ?? "loading")}
      meta={<span className="mono muted small">{quoteId}</span>}
    >
      {query.isPending && <p role="status">Loading options...</p>}
      {query.isError && !quote && (
        <p className="alert alert-error" role="alert">
          {describeError(query.error).message}
        </p>
      )}

      {quote && (
        <>
          {open && !expired && !dead && <Countdown expiresAt={quote.expiresAt} active label="Options expire" testId="countdown" />}

          {dead && (
            <p className="alert alert-info" role="status">
              A newer set of options replaced these.
            </p>
          )}
          {expired && !dead && (
            <div className="alert alert-warn" role="status" data-testid="quote-expired">
              <p>These options expired, so they can no longer be confirmed.</p>
              <button type="button" className="btn btn-small" onClick={() => onRequote(quote)} disabled={busy}>
                Ask for new options
              </button>
            </div>
          )}

          {primary && (
            <OptionBlock quote={quote} option={primary} primary disabled={disabled} approved={approvedId === primary.optionId} onChoose={onChoose} />
          )}
          {busy && !locked && !expired && !dead && <p className="muted small">Available when your personal agent finishes.</p>}

          {others.length > 0 && !dead && (
            <div className="others">
              <button
                type="button"
                className="btn btn-link others-toggle"
                aria-expanded={showOthers}
                aria-controls={panelId}
                onClick={() => setShowOthers((v) => !v)}
                data-testid="see-other-options"
              >
                <Icon name={showOthers ? "chevron-down" : "chevron-right"} size={16} />
                See other options ({others.length})
              </button>
              <div id={panelId} hidden={!showOthers} className="others-panel">
                {others.map((o) => (
                  <OptionBlock key={o.optionId} quote={quote} option={o} primary={false} disabled={disabled} approved={approvedId === o.optionId} onChoose={onChoose} />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </CardFrame>
  );
}
