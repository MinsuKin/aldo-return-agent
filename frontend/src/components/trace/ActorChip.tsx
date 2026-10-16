import { ACTOR_TITLE, actorLabel } from "../../lib/actors";
import { Icon, type IconName } from "../Icon";

/** The small inline-SVG glyph in each actor's icon square. */
const ACTOR_ICON: Record<string, IconName> = {
  personal_agent: "user",
  aldo_agent: "handoff",
  aldo_gateway: "signal",
  aldo_store: "store",
  system: "flag",
};

/**
 * Who sent an event: a 16px coloured icon square + the display name, rendered as one cohesive chip
 * in the actor's hue on its soft tint. The technical id stays in `data-actor` and the tooltip.
 */
export function ActorChip({ actor }: { actor: string }) {
  return (
    <span className={`actor-chip actor-${actor}`} title={ACTOR_TITLE[actor] ?? actor} data-testid="actor-chip" data-actor={actor}>
      <span className="actor-node" aria-hidden="true">
        <Icon name={ACTOR_ICON[actor] ?? "flag"} size={10} />
      </span>
      {actorLabel(actor)}
    </span>
  );
}
