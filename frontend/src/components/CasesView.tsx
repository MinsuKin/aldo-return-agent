import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { describeError } from "../lib/errors";
import { normalizeCase } from "../lib/normalize";
import { SimulatedBadge } from "./Badge";
import { CaseRow } from "./ResultPanels";

/** Minimal read-only list of the signed-in customer's escalation cases. */
export function CasesView() {
  const cases = useQuery({ queryKey: ["cases", "list"], queryFn: api.cases, retry: 1, refetchOnMount: "always" });
  const rows = (cases.data ?? []).map(normalizeCase);

  return (
    <section className="card page-card" aria-labelledby="cases-title" data-testid="cases-view">
      <div className="card-head">
        <h2 id="cases-title">Cases</h2>
        <SimulatedBadge />
      </div>
      <p className="muted">
        Cases the ALDO Returns Agent sent to a person for review. Read-only in this prototype, and the agent never promises an
        outcome.
      </p>

      {cases.isPending && <p role="status">Loading cases...</p>}
      {cases.isError && (
        <div className="alert alert-error" role="alert">
          <p>{describeError(cases.error).message}</p>
          <button type="button" className="btn" onClick={() => void cases.refetch()}>
            Try again
          </button>
        </div>
      )}
      {cases.data && rows.length === 0 && (
        <p data-testid="cases-empty">No cases yet. When the ALDO Returns Agent escalates a request, it appears here.</p>
      )}
      {rows.length > 0 && (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Cases table">
          <table className="table">
            <caption className="sr-only">Your cases</caption>
            <thead>
              <tr>
                <th scope="col">Case</th>
                <th scope="col">Reason</th>
                <th scope="col">Order</th>
                <th scope="col">Status</th>
                <th scope="col">Opened</th>
                <th scope="col">Summary</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <CaseRow key={c.caseId} c={c} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
