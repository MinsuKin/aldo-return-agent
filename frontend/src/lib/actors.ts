/** Display names for trace actors (user decision): ids stay technical, labels are what people see. */
export const ACTOR_LABEL: Record<string, string> = {
  personal_agent: "ALDO bot",
  aldo_gateway: "ALDO Gateway",
  aldo_agent: "ALDO Returns Agent",
  system: "System",
};

export const ACTOR_TITLE: Record<string, string> = {
  personal_agent: "ALDO bot, the owner's personal shopping agent (actor personal_agent)",
  aldo_gateway: "ALDO Gateway, ALDO's deterministic fit and stock service (actor aldo_gateway)",
  aldo_agent: "ALDO Returns Agent, ALDO's returns assistant (actor aldo_agent)",
  system: "System events (actor system)",
};

export const actorLabel = (actor: string): string => ACTOR_LABEL[actor] ?? actor;
