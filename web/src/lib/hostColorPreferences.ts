import {
  AUTO_HOST_COLOR,
  isHostColorKey,
  type HostColorKey,
  type HostColorPreferences,
} from "./hostColors";
import { queueUserPreferencePatch } from "./userPreferencesSync";

export const HOST_COLORS_STORAGE_KEY = "omnigent:host-colors";
export const HOST_COLORS_CHANGED_EVENT = "omnigent:host-colors-changed";

/**
 * Sanitize persisted or imported preferences: unknown host ids and palette
 * keys are dropped, so a value written by a newer build cannot leak an
 * unusable colour into the UI. The ``"auto"`` tombstone survives: it is how
 * a reset travels through the preferences server's shallow merge.
 */
export function normalizeHostColorPreferences(value: unknown): HostColorPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const normalized: HostColorPreferences = {};
  for (const [hostId, key] of Object.entries(value)) {
    if (!hostId) continue;
    if (key !== AUTO_HOST_COLOR && !isHostColorKey(key)) continue;
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
 * The Server sync patch carries the full local map — the localStorage
 * mirror, which server hydration replaces. Acknowledged edits another
 * client has since changed are therefore never replayed, and an account
 * switch cannot leak the previous account's keys. A reset travels as the
 * ``"auto"`` tombstone because ``patch_namespace`` shallow-merges and
 * cannot delete a key.
 */
export function patchHostColor(hostId: string, key: HostColorKey | null): void {
  if (typeof window === "undefined") return;
  const next = { ...readHostColorPreferences(), [hostId]: key ?? AUTO_HOST_COLOR };
  try {
    window.localStorage.setItem(HOST_COLORS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(HOST_COLORS_CHANGED_EVENT));
  queueUserPreferencePatch("host_colors", next);
}
