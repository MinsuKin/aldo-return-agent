import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatDateTime } from "../lib/format";
import { describeError } from "../lib/errors";
import { normalizeCase, type CaseView } from "../lib/normalize";

function CaseRow({ c }: { c: CaseView }) {
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

/** Minimal read-only list of the signed-in customer's cases that the personal agent passed to a person. */
export function CasesView() {
  const cases = useQuery({ queryKey: ["cases", "list"], queryFn: api.cases, retry: 1, refetchOnMount: "always" });
  const rows = (cases.data ?? []).map(normalizeCase);

  return (
    <section className="page-card" aria-labelledby="cases-title" data-testid="cases-view">
      <p>
        <a href="#/" className="back-link">
          Back to personal agent
        </a>
      </p>
      <h2 id="cases-title">Cases</h2>
      <p className="muted">
        Requests your personal agent passed to a person for review. This list is read-only, and your personal agent never promises an outcome.
      </p>

      {cases.isPending && <p role="status">Loading cases...</p>}
      {cases.isError && (
        <div className="alert alert-error" role="alert">
          <p>{describeError(cases.error).message}</p>
          <button type="button" className="btn btn-small" onClick={() => void cases.refetch()}>
            Try again
          </button>
        </div>
      )}
      {cases.data && rows.length === 0 && <p data-testid="cases-empty">No cases yet. When your personal agent passes a request to a person, it appears here.</p>}
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
