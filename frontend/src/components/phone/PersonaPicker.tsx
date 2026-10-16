import { useMemo } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { setSession } from "../../api/session";
import type { Persona } from "../../api/types";
import { describeError } from "../../lib/errors";
import { normalizePersona, type PersonaView } from "../../lib/normalize";
import { isV3 } from "../../lib/v3";
import { TierBadge } from "../Badge";
import { AdminControls } from "./AdminControls";
import { BotAvatar, PersonaAvatar } from "./Avatars";

/**
 * The persona's cohort as the server sent it (spec demo-stories-v3): `PersonaView.profile` once the view carries it,
 * else the raw `agent_profile` of the persona list. Read defensively, so it works with or without either field.
 */
function sentProfile(view: PersonaView, raw: Persona | undefined): string | undefined {
  if ("profile" in view && typeof view.profile === "string") return view.profile;
  return raw && "agent_profile" in raw && typeof raw.agent_profile === "string" ? raw.agent_profile : undefined;
}

interface Props {
  /** ?admin=1: also list the regression customers and show the reset controls. */
  admin: boolean;
  notice?: string;
  onReset: () => void;
}

/** The first screen inside the phone: who is using the personal agent (addendum 11.1 D1, task 2a). */
export function PersonaPicker({ admin, notice, onReset }: Props) {
  const personas = useQuery({ queryKey: ["personas"], queryFn: api.personas, retry: 1 });
  const signIn = useMutation({
    mutationFn: async ({ p, agentProfile }: { p: PersonaView; agentProfile?: string }) => {
      const session = await api.signIn(p.customerId);
      // Keep the tier fields (and the cohort) from the persona list when the session response does not repeat them.
      const fromList = {
        home_region: p.homeRegion,
        tier: p.tier,
        tier_label: p.tierLabel,
        location_label: p.locationLabel,
        ...(agentProfile ? { agent_profile: agentProfile } : {}),
      };
      return { ...session, customer: { ...fromList, ...session.customer } };
    },
    onSuccess: (session) => setSession(session),
  });

  const views = useMemo(() => (personas.data ?? []).map(normalizePersona), [personas.data]);
  const rawById = useMemo(() => new Map((personas.data ?? []).map((r) => [r.customer_id, r] as const)), [personas.data]);
  // The server's cohort field, else "v3" for the pilot ids (Bangos, Dusky); nothing for the others when the server sends none.
  const agentProfileOf = (v: PersonaView): string | undefined => sentProfile(v, rawById.get(v.customerId)) ?? (isV3(v.customerId) ? "v3" : undefined);
  // John and David (tiered) and the v3 cohort are the stage personas. If the server marks none, never leave the picker empty.
  const onStage = (v: PersonaView) => v.isDemo || isV3(v.customerId, agentProfileOf(v));
  const demo = views.some(onStage) ? views.filter(onStage) : views;
  const regression = views.filter((v) => !demo.includes(v));

  const card = (p: PersonaView) => {
    const agentProfile = agentProfileOf(p);
    const v3 = isV3(p.customerId, agentProfile);
    return (
      <li key={p.customerId}>
        <button
          type="button"
          className="persona-card"
          disabled={signIn.isPending}
          onClick={() => signIn.mutate({ p, agentProfile })}
          data-testid={`persona-${p.customerId}`}
        >
          <PersonaAvatar name={p.displayName} customerId={p.customerId} profile={v3 ? "v3" : undefined} />
          <span className="persona-text">
            <span className="persona-name">{p.displayName}</span>
            {p.locationLabel && <span className="persona-where">{p.locationLabel}</span>}
            {/* The v3 cohort's one-line story ("5 min from ALDO Sainte-Catherine · ..."). */}
            {v3 && p.description && <span className="persona-story">{p.description}</span>}
          </span>
          <TierBadge tier={p.tier} label={p.tierLabel} />
        </button>
      </li>
    );
  };

  return (
    <div className="screen picker" data-testid="persona-picker">
      <div className="picker-head">
        <BotAvatar size={64} />
        <h2 id="picker-title">Who is using the personal agent?</h2>
        <p className="muted">Pick a customer. The personal agent only sees that customer&apos;s own data.</p>
      </div>

      {notice && (
        <p className="alert alert-info" role="status">
          {notice}
        </p>
      )}
      {personas.isPending && <p role="status">Loading customers...</p>}
      {personas.isError && (
        <div className="alert alert-error" role="alert">
          <p>{describeError(personas.error).message}</p>
          <button type="button" className="btn btn-small" onClick={() => void personas.refetch()}>
            Try again
          </button>
        </div>
      )}

      {personas.data && (
        <ul className="persona-cards" aria-labelledby="picker-title">
          {demo.map(card)}
        </ul>
      )}

      {admin && personas.data && regression.length > 0 && (
        <section className="picker-admin" aria-labelledby="regression-title">
          <h3 id="regression-title">Regression customers</h3>
          <ul className="persona-cards persona-cards-compact">{regression.map(card)}</ul>
        </section>
      )}

      {signIn.isError && (
        <p className="alert alert-error" role="alert">
          {describeError(signIn.error).message}
        </p>
      )}

      {admin && <AdminControls onReset={onReset} />}
    </div>
  );
}
