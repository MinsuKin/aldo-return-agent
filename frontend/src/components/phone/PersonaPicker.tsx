import { useMemo } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { setSession } from "../../api/session";
import { describeError } from "../../lib/errors";
import { normalizePersona, type PersonaView } from "../../lib/normalize";
import { TierBadge } from "../Badge";
import { AdminControls } from "./AdminControls";
import { BotAvatar, PersonaAvatar } from "./Avatars";

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
    mutationFn: async (p: PersonaView) => {
      const session = await api.signIn(p.customerId);
      // Keep the tier fields from the persona list when the session response does not repeat them.
      const fromList = { home_region: p.homeRegion, tier: p.tier, tier_label: p.tierLabel, location_label: p.locationLabel };
      return { ...session, customer: { ...fromList, ...session.customer } };
    },
    onSuccess: (session) => setSession(session),
  });

  const views = useMemo(() => (personas.data ?? []).map(normalizePersona), [personas.data]);
  // If the server does not mark any customer as a tiered demo persona, never leave the picker empty.
  const demo = views.some((v) => v.isDemo) ? views.filter((v) => v.isDemo) : views;
  const regression = views.filter((v) => !demo.includes(v));

  const card = (p: PersonaView) => (
    <li key={p.customerId}>
      <button type="button" className="persona-card" disabled={signIn.isPending} onClick={() => signIn.mutate(p)} data-testid={`persona-${p.customerId}`}>
        <PersonaAvatar name={p.displayName} customerId={p.customerId} />
        <span className="persona-text">
          <span className="persona-name">{p.displayName}</span>
          {p.locationLabel && <span className="persona-where">{p.locationLabel}</span>}
        </span>
        <TierBadge tier={p.tier} label={p.tierLabel} />
      </button>
    </li>
  );

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
