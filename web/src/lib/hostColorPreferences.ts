import { isHostColorKey, type HostColorKey, type HostColorPreferences } from "./hostColors";
import { queueUserPreferencePatch } from "./userPreferencesSync";

export const HOST_COLORS_STORAGE_KEY = "omnigent:host-colors";
export const HOST_COLORS_CHANGED_EVENT = "omnigent:host-colors-changed";

/**
 * Sanitize persisted or imported preferences: unknown host ids and palette
 * keys are dropped, so a value written by a newer build (or a reset null)
 * cannot leak an unusable colour into the UI.
 */
export function normalizeHostColorPreferences(value: unknown): HostColorPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const normalized: HostColorPreferences = {};
  for (const [hostId, key] of Object.entries(value)) {
    if (!hostId || !isHostColorKey(key)) continue;
    normalized[hostId] = key;
  }
  return normalized;
}

export function readHostColorPreferences(): HostColorPreferences {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(HOST_COLORS_STORAGE_KEY);
    return raw ? normalizeHostColorPreferences(JSON.parse(raw)) : {};
  } catch {
    return {};
  }
}

/**
 * Set or clear one host's colour.
 *
 * The Server sync is a ONE-KEY patch: ``patch_namespace`` shallow-merges the
 * top-level map, so concurrent edits of different hosts never overwrite each
 * other, and a reset is an explicit ``null`` for that key. The localStorage
 * mirror keeps the full map for offline reads.
 */
export function patchHostColor(hostId: string, key: HostColorKey | null): void {
  if (typeof window === "undefined") return;
  const next = { ...readHostColorPreferences() };
  if (key === null) Reflect.deleteProperty(next, hostId);
  else next[hostId] = key;
  try {
    if (Object.keys(next).length === 0) window.localStorage.removeItem(HOST_COLORS_STORAGE_KEY);
    else window.localStorage.setItem(HOST_COLORS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(HOST_COLORS_CHANGED_EVENT));
  queueUserPreferencePatch("host_colors", { [hostId]: key });
}
