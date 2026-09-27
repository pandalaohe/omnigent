// localStorage-backed lists of what the user has actually launched. Lets the
// picker promote a harness someone uses regularly (Pi, Cursor) into the primary
// list alongside the fully supported ones, instead of leaving it behind "More"
// forever.
//
// One implementation, several keys: native / ACP harness launches live under
// `omnigent:recent-harnesses`, standalone SDK product launches under
// `omnigent:recent-sdk` (harness ids), and composed / saved Agent launches
// under `omnigent:recent-agents` (agent ids). Separate keys keep the two Codex
// rows — native `codex-native` and the SDK's bare `codex` — from ranking each
// other's group.

import { useCallback, useSyncExternalStore } from "react";

/** Native / ACP harness ids (e.g. `pi-native`). */
const RECENT_HARNESSES_KEY = "omnigent:recent-harnesses";
/** Standalone SDK product harness ids (`claude-sdk`, `codex`). */
export const RECENT_SDK_KEY = "omnigent:recent-sdk";
/** Composed built-in / saved (`ca_`) Agent ids. */
export const RECENT_AGENTS_KEY = "omnigent:recent-agents";
const MAX_RECENT_IDS = 4;
const emptySnapshot: string[] = [];

interface RecentStore {
  listeners: Set<() => void>;
  cachedRaw: string | null | undefined;
  snapshot: string[];
}

const stores = new Map<string, RecentStore>();

function storeFor(storageKey: string): RecentStore {
  let store = stores.get(storageKey);
  if (store === undefined) {
    store = { listeners: new Set(), cachedRaw: undefined, snapshot: emptySnapshot };
    stores.set(storageKey, store);
  }
  return store;
}

/** Most-recent-first stored ids for one key, e.g. ``["pi-native", …]``. */
function readSnapshot(store: RecentStore, storageKey: string): string[] {
  if (typeof window === "undefined") return emptySnapshot;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(storageKey);
  } catch {
    raw = null;
  }
  if (raw === store.cachedRaw) return store.snapshot;
  store.cachedRaw = raw;
  try {
    if (!raw) return (store.snapshot = []);
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return (store.snapshot = []);
    // Drop anything malformed so a corrupted entry can't crash the picker.
    return (store.snapshot = parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return (store.snapshot = []);
  }
}

function writeAll(storageKey: string, list: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(list));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recents just stop
    // persisting until the next successful write.
  }
}

interface RecentIds {
  /** Most-recent-first ids the user has launched. */
  recentIds: string[];
  /**
   * Record ``id`` as the newest launched entry. De-duplicates (moves an
   * existing entry to the front) and caps the list. No-op for a blank id.
   */
  addRecentId: (id: string) => void;
}

/**
 * Track the launched ids persisted under one storage key.
 *
 * Not host-scoped, unlike recent workspaces: a preference for Pi follows the
 * person across machines, whereas a workspace path is meaningful on one host.
 * Listener registration is per key, so writing one kind never re-renders
 * consumers of another.
 *
 * @param storageKey - One of the ``RECENT_*_KEY`` constants.
 * @returns The recent ids plus an ``addRecentId`` recorder.
 */
export function useRecentIds(storageKey: string): RecentIds {
  const store = storeFor(storageKey);
  const subscribeKey = useCallback(
    (listener: () => void) => {
      store.listeners.add(listener);
      return () => store.listeners.delete(listener);
    },
    [store],
  );
  const getSnapshot = useCallback(() => readSnapshot(store, storageKey), [store, storageKey]);
  const recentIds = useSyncExternalStore(subscribeKey, getSnapshot, () => emptySnapshot);

  const addRecentId = useCallback(
    (id: string) => {
      const trimmed = id.trim();
      if (!trimmed) return;
      const existing = readSnapshot(store, storageKey);
      // Already the newest entry → nothing to reorder, so skip the write and the
      // re-render it would trigger (the common case: relaunching the same entry).
      if (existing[0] === trimmed) return;
      writeAll(
        storageKey,
        [trimmed, ...existing.filter((entry) => entry !== trimmed)].slice(0, MAX_RECENT_IDS),
      );
      store.listeners.forEach((listener) => listener());
    },
    [store, storageKey],
  );

  return { recentIds, addRecentId };
}

export interface RecentHarnesses {
  /** Most-recent-first harness ids the user has launched. */
  recentHarnesses: string[];
  /**
   * Record ``harness`` as the newest launched harness. De-duplicates (moves an
   * existing entry to the front) and caps the list. No-op for a blank id.
   */
  addRecentHarness: (harness: string) => void;
}

/**
 * Track which native / ACP harnesses this user actually launches.
 *
 * @returns The recent harness ids plus an ``addRecentHarness`` recorder.
 */
export function useRecentHarnesses(): RecentHarnesses {
  const { recentIds, addRecentId } = useRecentIds(RECENT_HARNESSES_KEY);
  return { recentHarnesses: recentIds, addRecentHarness: addRecentId };
}
