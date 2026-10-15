/**
 * Display names for trace actors (user decisions): ids stay technical, labels are what people see.
 * ALDO bot is the one brand; purchase and return are its two workflows. The gateway reads as "ALDO Data".
 */
export const ACTOR_LABEL: Record<string, string> = {
  personal_agent: "ALDO bot · Purchase",
  aldo_agent: "ALDO bot · Return",
  aldo_gateway: "ALDO Data",
  aldo_store: "ALDO Store Desk",
  system: "System",
};

export const ACTOR_TITLE: Record<string, string> = {
  personal_agent: "ALDO bot, purchase workflow: shops for the owner and remembers their sizes (actor personal_agent)",
  aldo_agent: "ALDO bot, return workflow: handles exchanges and returns (actor aldo_agent)",
  aldo_gateway: "ALDO Data: fit signals, reviews, stock and delivery options (actor aldo_gateway)",
  aldo_store: "ALDO Store Desk: the store that gets notified (actor aldo_store)",
  system: "System events (actor system)",
};

/** Order shown in the legend. */
export const ACTOR_ORDER = ["personal_agent", "aldo_agent", "aldo_gateway", "aldo_store", "system"] as const;

export const actorLabel = (actor: string): string => ACTOR_LABEL[actor] ?? actor;
