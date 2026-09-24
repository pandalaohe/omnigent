// Pinned Agents: the picker's "Agents" group shows at most these at its top
// level and folds every other Agent into a submenu. localStorage is the
// synchronous read path; userPreferencesSync hydrates and patches the server.

import { useCallback, useEffect, useState } from "react";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { queueUserPreferencePatch } from "./userPreferencesSync";

export const MAX_PINNED_AGENTS = 3;

const STORAGE_KEY = "omnigent:agent-pins";
const CHANGED_EVENT = "omnigent:agent-pins-changed";

// Names of the shipped defaults: a fresh account starts on Polly + Debby.
const DEFAULT_PINNED_AGENT_NAMES = ["polly", "debby"] as const;

export interface AgentPins {
  /** Stored pin ids, or `null` when the user never set a list (→ defaults). */
  storedIds: string[] | null;
  setPinnedIds: (ids: readonly string[]) => void;
}

/** Keep only non-empty strings, de-duplicated, in order, capped at the max. */
function normalizePinnedIds(values: readonly unknown[]): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const value of values) {
    if (typeof value !== "string" || value === "" || seen.has(value)) continue;
    seen.add(value);
    ids.push(value);
    if (ids.length === MAX_PINNED_AGENTS) break;
  }
  return ids;
}

/** Read the stored pin list; a malformed value counts as never set. */
function readStoredPinnedAgentIds(): string[] | null {
  if (typeof window === "undefined") return null;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const ids = (parsed as { ids?: unknown }).ids;
  return Array.isArray(ids) ? normalizePinnedIds(ids) : null;
}

/** Store the list, dispatch the category event, and queue the sync patch. */
function writePinnedIds(ids: readonly string[]): void {
  const normalized = normalizePinnedIds(ids);
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: normalized }));
  } catch {
    // Storage quota / access errors must not break pinning.
  }
  window.dispatchEvent(new Event(CHANGED_EVENT));
  queueUserPreferencePatch("agent_pins", { ids: normalized });
}

/** Resolve pins against a surface's agents; `null` (never set) → the default pair. */
export function resolvePinnedAgentIds(
  storedIds: readonly string[] | null,
  agents: readonly Pick<AvailableAgent, "id" | "name" | "builtin">[],
): string[] {
  if (storedIds !== null) {
    const present = new Set(agents.map((agent) => agent.id));
    return storedIds.filter((id) => present.has(id));
  }
  return DEFAULT_PINNED_AGENT_NAMES.map((name) =>
    agents.find((agent) => agent.builtin !== false && agent.name === name),
  )
    .filter(
      (agent): agent is Pick<AvailableAgent, "id" | "name" | "builtin"> => agent !== undefined,
    )
    .map((agent) => agent.id);
}

/** Drop one id from the current stored list; a missing list or id is a no-op. */
export function unpinAgent(id: string): void {
  const storedIds = readStoredPinnedAgentIds();
  if (storedIds === null || !storedIds.includes(id)) return;
  writePinnedIds(storedIds.filter((storedId) => storedId !== id));
}

// Subscribe to the pin list: `storedIds` follows the category and `storage`
// events; `setPinnedIds` stores, dispatches, and queues the sync patch.
export function useAgentPins(): AgentPins {
  const [storedIds, setStoredIds] = useState<string[] | null>(readStoredPinnedAgentIds);
  useEffect(() => {
    const sync = () => setStoredIds(readStoredPinnedAgentIds());
    window.addEventListener(CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const setPinnedIds = useCallback((ids: readonly string[]) => writePinnedIds(ids), []);
  return { storedIds, setPinnedIds };
}
