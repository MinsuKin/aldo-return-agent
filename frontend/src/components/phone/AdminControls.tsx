import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { DataMode, DataPlatformStatus, ResetVariant } from "../../api/types";
import { describeError } from "../../lib/errors";

const VARIANTS: { id: ResetVariant; label: string; help: string }[] = [
  { id: "default", label: "Reset: default", help: "DC has stock" },
  { id: "dc_stockout", label: "Reset: dc_stockout", help: "DC out, a store has stock" },
  { id: "all_stockout", label: "Reset: all_stockout", help: "No stock anywhere" },
];

const DATA_MODES: { id: DataMode; label: string; help: string }[] = [
  { id: "static", label: "Static", help: "The bundled file (today's demo)" },
  { id: "platform", label: "Platform", help: "The ALDO dataset analysed on AWS (S3, Glue, Athena); falls back to static by itself" },
  { id: "shadow", label: "Shadow", help: "Serve static, compare with the platform in the background" },
];

/** Hidden demo controls, shown only with ?admin=1. The secret is typed here and kept in memory only. */
export function AdminControls({ onReset }: { onReset: () => void }) {
  const [secret, setSecret] = useState("");
  const [data, setData] = useState<DataPlatformStatus | null>(null);
  const checkData = useMutation({ mutationFn: () => api.dataPlatform(secret), onSuccess: setData });
  const flipData = useMutation({ mutationFn: (mode: DataMode) => api.setDataPlatform(mode, secret), onSuccess: setData });
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
        <div role="group" aria-label="Data source for fit insights" className="admin-data" data-testid="data-platform">
          <p className="admin-data-title">Data source for fit insights</p>
          <div className="admin-buttons">
            <button type="button" className="btn btn-small" disabled={checkData.isPending || secret === ""} onClick={() => checkData.mutate()} data-testid="data-platform-check">
              Check
            </button>
            {DATA_MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                className={`btn btn-small${data?.mode === m.id ? " is-current" : ""}`}
                aria-pressed={data?.mode === m.id}
                disabled={flipData.isPending || secret === "" || data === null}
                onClick={() => flipData.mutate(m.id)}
                data-testid={`data-mode-${m.id}`}
                title={m.help}
              >
                {m.label}
              </button>
            ))}
          </div>
          <p role="status" className="admin-status small" data-testid="data-platform-status">
            {(checkData.isPending || flipData.isPending) && "Working..."}
            {data !== null &&
              !checkData.isPending &&
              !flipData.isPending &&
              `Source: ${data.mode}. ${
                data.available && data.curated
                  ? `AWS data ready (${data.curated.products.toLocaleString("en-CA")} products, ${data.curated.generated_at.slice(0, 16).replace("T", " ")} UTC).`
                  : "AWS data not available: static is used."
              }`}
          </p>
          {(checkData.isError || flipData.isError) && (
            <p className="alert alert-error" role="alert">
              {describeError(checkData.error ?? flipData.error).message}
            </p>
          )}
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
