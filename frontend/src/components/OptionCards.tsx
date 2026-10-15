import { useEffect, useRef } from "react";
import { useNow } from "../hooks/useNow";
import { formatCents, formatCountdown, formatDate } from "../lib/format";
import type { OptionView, QuoteView } from "../lib/normalize";
import { Badge, ProvenanceBadge, SimulatedBadge } from "./Badge";
import { Icon } from "./Icon";

interface Props {
  quote: QuoteView | undefined;
  quoteId: string;
  loading: boolean;
  error: string | null;
  runActive: boolean;
  /** Disables the re-quote button while a request is in flight. */
  requoting: boolean;
  onApprove: (option: OptionView, trigger: HTMLElement) => void;
  onRequote: () => void;
}

function etaText(o: OptionView): string {
  if (o.etaDate) return formatDate(o.etaDate);
  return o.etaText ?? (o.type === "return_in_store" ? "At drop-off" : "Not provided");
}

function refundText(o: OptionView): string {
  if (o.isExchange && o.refundCents === 0) return "None, this is a size exchange";
  return formatCents(o.refundCents, o.currency);
}

export function OptionCards({ quote, quoteId, loading, error, runActive, requoting, onApprove, onRequote }: Props) {
  const open = quote?.status === "open";
  const now = useNow(!!quote && open);
  const remaining = quote?.expiresAt ? quote.expiresAt - now : null;
  const expired = quote ? quote.status === "expired" || (open && remaining !== null && remaining <= 0) : false;
  const approvedId = quote?.approvedOptionId;
  const locked = !!approvedId || quote?.status === "executed";
  const dead = quote?.status === "superseded";

  // Bring the options into view once per quote, when the agent has finished (the column scrolls on its own).
  const headingRef = useRef<HTMLHeadingElement>(null);
  const revealed = useRef<string | null>(null);
  const ready = !!quote && !runActive;
  useEffect(() => {
    if (!ready || revealed.current === quoteId) return;
    revealed.current = quoteId;
    headingRef.current?.scrollIntoView({ block: "start" });
  }, [ready, quoteId]);

  return (
    <section className="card" aria-labelledby="options-title" data-testid="options">
      <div className="card-head">
        <h3 id="options-title" ref={headingRef} tabIndex={-1}>
          Your options
        </h3>
        <span className="muted mono">{quoteId}</span>
      </div>

      {loading && <p role="status">Loading options from the server...</p>}
      {error && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}

      {quote && (
        <>
          <p className="muted options-meta">
            Amounts come from the server, not from the agent&apos;s text.
            {quote.policyVersion ? ` Policy ${quote.policyVersion} (demo assumption).` : ""}
            {open && remaining !== null && remaining > 0 && (
              <>
                {" "}
                <span className="countdown" data-testid="countdown">
                  <Icon name="clock" size={14} /> Expires in {formatCountdown(remaining)}
                </span>
              </>
            )}
          </p>

          {dead && (
            <p className="alert alert-info" role="status">
              A newer set of options replaced these. Use the latest options.
            </p>
          )}

          {expired && !dead && (
            <div className="alert alert-warn" role="status" data-testid="quote-expired">
              <p>These options expired, so they can no longer be approved.</p>
              <button type="button" className="btn" onClick={onRequote} disabled={runActive || requoting}>
                Ask for new options
              </button>
            </div>
          )}

          <ul className="option-list">
            {quote.options.map((o) => {
              const isApproved = approvedId === o.optionId;
              const disabled = runActive || expired || locked || dead;
              return (
                <li key={o.optionId}>
                  <article
                    className={`option-card${isApproved ? " is-approved" : ""}`}
                    aria-labelledby={`opt-title-${o.optionId}`}
                    data-testid={`option-card-${o.optionId}`}
                    data-option-type={o.type}
                  >
                    <header className="option-head">
                      <h4 id={`opt-title-${o.optionId}`}>{o.typeLabel}</h4>
                      <div className="chips">
                        <SimulatedBadge />
                        {o.meetsDeadline === true && (
                          <Badge tone="success">
                            <Icon name="check" size={14} /> Meets your deadline
                          </Badge>
                        )}
                        {o.meetsDeadline === false && <Badge tone="neutral">After your deadline</Badge>}
                        {isApproved && <Badge tone="success">Approved</Badge>}
                      </div>
                    </header>

                    <dl className="facts">
                      {o.isExchange && (
                        <div>
                          <dt>Replacement</dt>
                          <dd>
                            {o.productName ?? "Same style"}
                            {o.replacementSize ? `, size ${o.replacementSize}` : ""}
                            {o.color ? `, ${o.color.toLowerCase()}` : ""}
                          </dd>
                        </div>
                      )}
                      <div>
                        <dt>Location</dt>
                        <dd>{o.locationName ?? "Not provided"}</dd>
                      </div>
                      <div>
                        <dt>Refund</dt>
                        <dd>{refundText(o)}</dd>
                      </div>
                      <div>
                        <dt>Fee</dt>
                        <dd>{formatCents(o.feeCents, o.currency)}</dd>
                      </div>
                      <div>
                        <dt>ETA</dt>
                        <dd>{etaText(o)}</dd>
                      </div>
                    </dl>

                    {o.conditions.length > 0 && (
                      <div className="conditions">
                        <h5>Conditions</h5>
                        <ul>
                          {o.conditions.map((c) => (
                            <li key={c}>{c}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {(o.evidence.length > 0 || o.provenance.length > 0) && (
                      <div className="evidence">
                        <h5>Evidence</h5>
                        <ul className="chips">
                          {o.evidence.map((c) => (
                            <li key={c.label} className="chip">
                              <span className="chip-label">{c.label}</span> {c.value}
                            </li>
                          ))}
                          {o.provenance.map((p) => (
                            <li key={p}>
                              <ProvenanceBadge kind={p} />
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <div className="option-actions">
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={disabled}
                        onClick={(e) => onApprove(o, e.currentTarget)}
                        data-testid={`approve-${o.optionId}`}
                        aria-label={`Approve: ${o.typeLabel}${o.locationName ? ` at ${o.locationName}` : ""}`}
                      >
                        Approve
                      </button>
                      {runActive && !locked && !expired && !dead && (
                        <span className="muted">Available when the ALDO Returns Agent finishes.</span>
                      )}
                    </div>
                  </article>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
