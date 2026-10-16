import { ACTOR_ORDER } from "../../lib/actors";
import { ActorChip } from "./ActorChip";

/**
 * The key to the trace: one chip per actor, in its own container above the trace panel so the panel
 * itself holds only the header, the short note and the events.
 */
export function TraceKey() {
  return (
    <div className="trace-key" data-testid="trace-key">
      <ul className="actor-legend" aria-label="Who sends each event">
        {ACTOR_ORDER.map((a) => (
          <li key={a}>
            <ActorChip actor={a} />
          </li>
        ))}
      </ul>
    </div>
  );
}
