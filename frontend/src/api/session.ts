import { useSyncExternalStore } from "react";
import type { SessionInfo } from "./types";

// Token lives in memory and in sessionStorage (cleared when the tab closes).
const KEY = "aldo.demo.session";
let current: SessionInfo | null = null;
let loaded = false;
const listeners = new Set<() => void>();

function isFresh(s: SessionInfo): boolean {
  const t = Date.parse(s.expires_at);
  return Number.isNaN(t) || t > Date.now();
}

function load(): SessionInfo | null {
  if (loaded) return current;
  loaded = true;
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as SessionInfo;
      if (parsed?.token && isFresh(parsed)) current = parsed;
      else sessionStorage.removeItem(KEY);
    }
  } catch {
    current = null;
  }
  return current;
}

export function getSession(): SessionInfo | null {
  return load();
}

export function setSession(s: SessionInfo | null): void {
  loaded = true;
  current = s;
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    // Storage can be unavailable (private mode); the in-memory copy still works.
  }
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useSession(): SessionInfo | null {
  return useSyncExternalStore(subscribe, getSession, () => null);
}
