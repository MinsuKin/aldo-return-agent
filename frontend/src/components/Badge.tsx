import type { ReactNode } from "react";
import type { Provenance } from "../api/types";
import { PROVENANCE_LABEL } from "../lib/normalize";

export type BadgeTone = "simulated" | "synthetic" | "derived" | "provided" | "success" | "neutral" | "amber" | "measured" | "assumed";

export function Badge({ tone, children, title }: { tone: BadgeTone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function SimulatedBadge() {
  return <Badge tone="simulated">Simulated data</Badge>;
}

export function ProvenanceBadge({ kind }: { kind: Provenance }) {
  const tone: BadgeTone = kind === "synthetic" ? "synthetic" : kind === "derived" ? "derived" : "provided";
  return <Badge tone={tone}>{PROVENANCE_LABEL[kind]}</Badge>;
}
