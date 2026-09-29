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

// Every host this page session changed, latest pick or null reset. The sync
// queue replaces a namespace's pending patch, so each call resends the union;
// an already-sent key is idempotent under the server's shallow merge.
const changedHostColors = new Map<string, HostColorKey | null>();

/**
 * Set or clear one host's colour.
 *
 * The Server sync patch carries every host key this page changed, each with
 * its latest value: ``patch_namespace`` shallow-merges the top-level map, so
 * a repeated key is idempotent and a reset travels as an explicit ``null``.
 * The localStorage mirror keeps the full map for offline reads.
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
  changedHostColors.set(hostId, key);
  queueUserPreferencePatch("host_colors", Object.fromEntries(changedHostColors));
}
