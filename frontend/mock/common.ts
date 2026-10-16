// Shared helpers of the scripted personal agent (purchase workflow in bot2.ts, return workflow in returns2.ts).

import type { MockPersona } from "./data.ts";
import { persona, type Conv, type RunKind, type Script } from "./state.ts";

/** Technical actor ids (the UI shows "Personal agent" and "ALDO agent"). */
export const BOT = "personal_agent";
export const RET = "aldo_agent";

export interface TurnParams {
  customerId: string;
  conv: Conv;
  runId: string;
  message: string;
}

/** What the API needs to start a run for an owner message. */
export interface Turn {
  script: Script;
  /** Run kind; it decides the default actor of events that carry none. */
  kind: RunKind;
  /** Which workflow the message was routed to when it was accepted. */
  agent: "personal" | "returns";
}

export interface Counter {
  round: number;
  tools: number;
}

/** John and David follow the demo script; everyone else is a regression customer. */
export type Who = "john" | "david" | "legacy";
export const whoIs = (customerId: string): Who => (customerId.endsWith("JOHN") ? "john" : customerId.endsWith("DAVID") ? "david" : "legacy");

export const meOf = (customerId: string): MockPersona => persona(customerId) as MockPersona;

export const money = (cents: number): string => `CAD ${(cents / 100).toFixed(2)}`;
export const cap = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();

export function say(s: Script, actor: string, text: string, delay = 520): void {
  s.emit(delay, "assistant_message", { text }, undefined, actor);
}
