import type { ReactNode } from "react";
import type { Provenance } from "../api/types";
import { PROVENANCE_LABEL } from "../lib/normalize";

export type BadgeTone = "synthetic" | "derived" | "provided" | "success" | "neutral" | "amber" | "silver" | "platinum";

export function Badge({ tone, children, title }: { tone: BadgeTone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** Provenance badge. Only shown where it adds credibility, for example "derived from provided ALDO data". */
export function ProvenanceBadge({ kind }: { kind: Provenance }) {
  const tone: BadgeTone = kind === "synthetic" ? "synthetic" : kind === "derived" ? "derived" : "provided";
  return <Badge tone={tone}>{PROVENANCE_LABEL[kind]}</Badge>;
}

/** Demo tier of a customer (addendum 11.1 D2), for example "Silver member". */
export function TierBadge({ tier, label }: { tier: string; label?: string }) {
  if (!tier || tier === "none") return null;
  const tone: BadgeTone = tier === "platinum" ? "platinum" : tier === "silver" ? "silver" : "neutral";
  return (
    <Badge tone={tone} title="Membership tier">
      {label ?? `${tier.charAt(0).toUpperCase()}${tier.slice(1)} member`}
    </Badge>
  );
}
