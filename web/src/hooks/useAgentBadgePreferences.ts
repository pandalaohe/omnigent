import { useSyncExternalStore } from "react";

import {
  AGENT_BADGE_CHANGED_EVENT,
  AGENT_BADGE_STORAGE_KEY,
  DEFAULT_AGENT_BADGE_PREFERENCES,
  readAgentBadgePreferences,
  type AgentBadgePreferences,
} from "@/lib/agentBadgePreferences";

const listeners = new Set<() => void>();
let listening = false;

function notify(): void {
  for (const listener of listeners) listener();
}

function onStorage(event: StorageEvent): void {
  if (event.key === null || event.key === AGENT_BADGE_STORAGE_KEY) notify();
}

function installListeners(): void {
  if (listening || typeof window === "undefined") return;
  window.addEventListener(AGENT_BADGE_CHANGED_EVENT, notify);
  window.addEventListener("storage", onStorage);
  listening = true;
}

function removeListeners(): void {
  if (!listening || typeof window === "undefined") return;
  window.removeEventListener(AGENT_BADGE_CHANGED_EVENT, notify);
  window.removeEventListener("storage", onStorage);
  listening = false;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  installListeners();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) removeListeners();
  };
}

export function useAgentBadgePreferences(): AgentBadgePreferences {
  return useSyncExternalStore(
    subscribe,
    readAgentBadgePreferences,
    () => DEFAULT_AGENT_BADGE_PREFERENCES,
  );
}
