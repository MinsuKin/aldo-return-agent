import { useEffect, useRef } from "react";
import type { CaseView, TransactionView } from "../lib/normalize";
import { formatDate, formatDateTime } from "../lib/format";
import { codeHint } from "../lib/errors";
import { SimulatedBadge } from "./Badge";
import { Icon } from "./Icon";

interface ConfirmationProps {
  txn: TransactionView | undefined;
  loading: boolean;
  error: string | null;
  /** Demo control for exchanges: reports the owner collected the pair and kept it. */
  onSimulatePickup: (transactionId: string) => void;
  pickupBusy: boolean;
  pickupDone: boolean;
  pickupError: string | null;
}

/** Confirmation panel for a persisted transaction (requirements 8.7 and 9.1). */
export function ConfirmationPanel({ txn, loading, error, onSimulatePickup, pickupBusy, pickupDone, pickupError }: ConfirmationProps) {
  const ref = useRef<HTMLHeadingElement>(null);
  const hasTxn = !!txn;
  useEffect(() => {
    // Move focus to the result once, when it first appears.
    if (hasTxn) ref.current?.focus();
  }, [hasTxn]);

  return (
    <section className="card panel-success" aria-labelledby="txn-title" data-testid="confirmation-panel">
      <div className="card-head">
        <h3 id="txn-title" ref={ref} tabIndex={-1}>
          {txn ? `Simulated ${txn.kind} created` : "Confirming with the server"}
        </h3>
        <SimulatedBadge />
      </div>
      {loading && !txn && <p role="status">Reading the saved record...</p>}
      {error && !txn && (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      )}
      {txn && (
        <>
          <dl className="facts facts-wide">
            <div>
              <dt>Reference</dt>
              <dd className="mono strong" data-testid="txn-id">
                {txn.transactionId}
              </dd>
            </div>
            <div>
              <dt>State</dt>
              <dd data-testid="txn-state">
                {txn.state} <span className="muted">(simulated {txn.kind} created)</span>
              </dd>
            </div>
            {txn.optionLabel && (
              <div>
                <dt>Type</dt>
                <dd>{txn.optionLabel}</dd>
              </div>
            )}
            {txn.replacementSize && (
              <div>
                <dt>Replacement size</dt>
                <dd>{txn.replacementSize}</dd>
              </div>
            )}
            {txn.locationName && (
              <div>
                <dt>Location</dt>
                <dd>{txn.locationName}</dd>
              </div>
            )}
            {txn.etaDate && (
              <div>
                <dt>ETA</dt>
                <dd>{formatDate(txn.etaDate)}</dd>
              </div>
            )}
            <div>
              <dt>Next step</dt>
              <dd data-testid="txn-next-step">{txn.nextStep ?? "No further action is required."}</dd>
            </div>
          </dl>
          <p className="note">
            In this prototype, &quot;completed&quot; means &quot;simulated exchange/return created&quot;, not refunded or shipped. It
            is a record in the demo database only.
          </p>
          {txn.kind === "exchange" && (
            <div className="demo-control">
              <button
                type="button"
                className="btn btn-demo"
                disabled={pickupBusy || pickupDone}
                onClick={() => onSimulatePickup(txn.transactionId)}
                data-testid="simulate-pickup"
              >
                {pickupBusy ? "Simulating..." : pickupDone ? "Pickup simulated" : "Simulate pickup"}
              </button>
              <span className="muted">Demo control: the owner collects the pair and keeps it, which confirms ALDO bot&apos;s size memory.</span>
            </div>
          )}
          {pickupError && (
            <p className="alert alert-error" role="alert">
              {pickupError}
            </p>
          )}
        </>
      )}
    </section>
  );
}

/** Shown when the agent escalates to a person (requirement 10). */
export function CasePanel({ caseId, view }: { caseId: string; view: CaseView | undefined }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, [caseId]);

  return (
    <section className="card panel-case" aria-labelledby="case-title" data-testid="case-panel">
      <div className="card-head">
        <h3 id="case-title" ref={ref} tabIndex={-1}>
          Sent to a person for review
        </h3>
        <SimulatedBadge />
      </div>
      <dl className="facts facts-wide">
        <div>
          <dt>Case ID</dt>
          <dd className="mono strong" data-testid="case-id">
            {caseId}
          </dd>
        </div>
        {view?.reasonCode && (
          <div>
            <dt>Reason</dt>
            <dd>{view.reasonCode}</dd>
          </div>
        )}
        {view?.summary && (
          <div>
            <dt>Summary</dt>
            <dd>{view.summary}</dd>
          </div>
        )}
        <div>
          <dt>Status</dt>
          <dd>{view?.status ?? "open"}</dd>
        </div>
      </dl>
      <p className="note">
        A person will review this case. The ALDO Returns Agent cannot promise an outcome. See all of your cases in the{" "}
        <a href="#/cases">Cases tab</a>.
      </p>
    </section>
  );
}

export function CaseRow({ c }: { c: CaseView }) {
  return (
    <tr>
      <th scope="row" className="mono">
        {c.caseId}
      </th>
      <td>{c.reasonCode || "n/a"}</td>
      <td className="mono nowrap">{c.orderId ?? "n/a"}</td>
      <td>{c.status}</td>
      <td>{formatDateTime(c.createdAt) || "n/a"}</td>
      <td>{c.summary || "n/a"}</td>
    </tr>
  );
}

export function ErrorCodeNote({ code }: { code: string }) {
  const hint = codeHint(code);
  return (
    <span className="code-amber" title={hint}>
      <Icon name="alert" size={14} /> {code}
    </span>
  );
}
