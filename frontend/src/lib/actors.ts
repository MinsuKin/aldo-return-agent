/**
 * Display names for trace actors (user decisions): ids stay technical, labels are what people see.
 * The owner chats with their personal agent, which works with the ALDO agent (returns). The gateway reads as "ALDO Data".
 */
export const ACTOR_LABEL: Record<string, string> = {
  personal_agent: "Personal agent",
  aldo_agent: "ALDO agent",
  aldo_gateway: "ALDO Data",
  aldo_store: "ALDO Store Desk",
  system: "System",
};

export const ACTOR_TITLE: Record<string, string> = {
  personal_agent: "Personal agent: shops for the owner and remembers their sizes (actor personal_agent)",
  aldo_agent: "ALDO agent: handles exchanges and returns (actor aldo_agent)",
  aldo_gateway: "ALDO Data: fit signals, reviews, stock and delivery options (actor aldo_gateway)",
  aldo_store: "ALDO Store Desk: the store that gets notified (actor aldo_store)",
  system: "System events (actor system)",
};

/** Order shown in the legend. */
export const ACTOR_ORDER = ["personal_agent", "aldo_agent", "aldo_gateway", "aldo_store", "system"] as const;

export const actorLabel = (actor: string): string => ACTOR_LABEL[actor] ?? actor;
