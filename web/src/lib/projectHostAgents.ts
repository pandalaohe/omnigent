import { useSyncExternalStore } from "react";
import { queueUserPreferencePatch } from "./userPreferencesSync";

const KEY = "omnigent:project-host-agents";
const EVENT = "omnigent:project-host-agents-changed";

function snapshot(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "{}";
  } catch {
    return "{}";
  }
}

function subscribe(listener: () => void): () => void {
  window.addEventListener(EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

function parse(raw: string): Record<string, string | null> {
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value).filter(
        ([, id]) => id === null || (typeof id === "string" && id.trim() !== ""),
      ),
    );
  } catch {
    return {};
  }
}

export function useProjectHostAgents(): Record<string, string | null> {
  return parse(useSyncExternalStore(subscribe, snapshot, () => "{}"));
}

export function setProjectHostAgent(hostId: string, agentId: string | null): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify({ ...parse(snapshot()), [hostId]: agentId }));
  } catch {
    // The server remains the authoritative preference store.
  }
  window.dispatchEvent(new Event(EVENT));
  queueUserPreferencePatch("project_host_agents", { [hostId]: agentId });
}
