import { useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { normalizeCase } from "../../../lib/normalize";
import { CardFrame, useFocusWhenReady } from "./shared";

/** Shown when ALDO bot passes a request to a person (requirement 10). */
export function CaseCard({ caseId }: { caseId: string }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const cases = useQuery({ queryKey: ["cases"], queryFn: api.cases, retry: 2 });
  const view = useMemo(() => {
    const raw = cases.data?.find((c) => c.case_id === caseId);
    return raw ? normalizeCase(raw) : undefined;
  }, [cases.data, caseId]);
  useFocusWhenReady(headingRef, true);

  return (
    <CardFrame
      title="Sent to a person for review"
      titleId={`case-title-${caseId}`}
      headingRef={headingRef}
      testId="case-card"
      tone="warn"
      meta={<span className="mono muted small" data-testid="case-id">{caseId}</span>}
    >
      <dl className="facts">
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
      <p className="muted small">
        A person will review this. ALDO bot cannot promise an outcome. See all cases in the <a href="#/cases">Cases list</a>.
      </p>
    </CardFrame>
  );
}
