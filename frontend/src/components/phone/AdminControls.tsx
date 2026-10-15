import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ResetVariant } from "../../api/types";
import { describeError } from "../../lib/errors";

const VARIANTS: { id: ResetVariant; label: string; help: string }[] = [
  { id: "default", label: "Reset: default", help: "DC has stock" },
  { id: "dc_stockout", label: "Reset: dc_stockout", help: "DC out, a store has stock" },
  { id: "all_stockout", label: "Reset: all_stockout", help: "No stock anywhere" },
];

/** Hidden demo controls, shown only with ?admin=1. The secret is typed here and kept in memory only. */
export function AdminControls({ onReset }: { onReset: () => void }) {
  const [secret, setSecret] = useState("");
  const reset = useMutation({
    mutationFn: (variant: ResetVariant) => api.reset(variant, secret),
    onSuccess: () => onReset(),
  });

  return (
    <details className="admin-controls" open data-testid="admin-panel">
      <summary>Demo controls (admin)</summary>
      <div className="admin-body">
        <div className="field">
          <label htmlFor="admin-secret">Admin secret</label>
          <input
            id="admin-secret"
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            aria-describedby="admin-help"
          />
          <span id="admin-help" className="muted small">
            Sent as the X-Demo-Admin header. Kept in memory only, never stored.
          </span>
        </div>
        <div role="group" aria-label="Reset the demo data" className="admin-buttons">
          {VARIANTS.map((v) => (
            <button
              key={v.id}
              type="button"
              className="btn btn-small"
              disabled={reset.isPending || secret === ""}
              onClick={() => reset.mutate(v.id)}
              data-testid={`reset-${v.id}`}
              title={v.help}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p role="status" className="admin-status small" data-testid="admin-status">
          {reset.isPending && "Resetting..."}
          {reset.isSuccess && `Reset to ${reset.variables} complete. Orders, runs, quotes, cases and memory were restored.`}
        </p>
        {reset.isError && (
          <p className="alert alert-error" role="alert">
            {describeError(reset.error).message}
          </p>
        )}
      </div>
    </details>
  );
}
