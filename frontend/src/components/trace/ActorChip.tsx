import { ACTOR_TITLE, actorLabel } from "../../lib/actors";

/** Who sent an event, by display name. The technical id stays in `data-actor` and the tooltip. */
export function ActorChip({ actor }: { actor: string }) {
  return (
    <span className={`actor actor-${actor}`} title={ACTOR_TITLE[actor] ?? actor} data-testid="actor-chip" data-actor={actor}>
      {actorLabel(actor)}
    </span>
  );
}
