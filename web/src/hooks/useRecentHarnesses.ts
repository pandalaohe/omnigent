// localStorage-backed set of harnesses the user has actually launched. Lets the
// picker promote a harness someone uses regularly (Pi, Cursor) into the primary
// list alongside the fully supported ones, instead of leaving it behind "More"
// forever.

import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:recent-harnesses";
const MAX_ENTRIES = 4;
const listeners = new Set<() => void>();
const emptySnapshot: string[] = [];
let cachedRaw: string | null | undefined;
let cachedSnapshot: string[] = emptySnapshot;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Most-recent-first canonical harness ids, e.g. ``["pi-native", …]``. */
function getSnapshot(): string[] {
  if (typeof window === "undefined") return emptySnapshot;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    raw = null;
  }
  if (raw === cachedRaw) return cachedSnapshot;
  cachedRaw = raw;
  try {
    if (!raw) return (cachedSnapshot = []);
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return (cachedSnapshot = []);
    // Drop anything malformed so a corrupted entry can't crash the picker.
    return (cachedSnapshot = parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return (cachedSnapshot = []);
  }
}

function writeAll(list: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recents just stop
    // persisting until the next successful write.
  }
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
 * Track which harnesses this user actually launches.
 *
 * Not host-scoped, unlike recent workspaces: a preference for Pi follows the
 * person across machines, whereas a workspace path is meaningful on one host.
 *
 * @returns The recent harness ids plus an ``addRecentHarness`` recorder.
 */
export function useRecentHarnesses(): RecentHarnesses {
  const recentHarnesses = useSyncExternalStore(subscribe, getSnapshot, () => emptySnapshot);

  const addRecentHarness = useCallback((harness: string) => {
    const trimmed = harness.trim();
    if (!trimmed) return;
    const existing = getSnapshot();
    // Already the newest entry → nothing to reorder, so skip the write and the
    // re-render it would trigger (the common case: relaunching the same harness).
    if (existing[0] === trimmed) return;
    writeAll([trimmed, ...existing.filter((h) => h !== trimmed)].slice(0, MAX_ENTRIES));
    listeners.forEach((listener) => listener());
  }, []);

  return { recentHarnesses, addRecentHarness };
}
