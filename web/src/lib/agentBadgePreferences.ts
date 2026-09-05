import { queueUserPreferencePatch } from "./userPreferencesSync";

export const AGENT_BADGE_STORAGE_KEY = "omnigent:agent-badge-preferences";
export const AGENT_BADGE_CHANGED_EVENT = "omnigent:agent-badge-preferences-changed";

const HEX_COLOR = /^#[0-9a-f]{6}$/i;
const MAX_AGENT_BADGES = 256;
const MAX_AGENT_ID_LENGTH = 512;

export interface AgentBadgeValue {
  label: string;
  borderColor: string;
  textColor: string;
}

export interface AgentBadgePreferences {
  version: 1;
  enabled: boolean;
  entries: Record<string, AgentBadgeValue>;
}

export const DEFAULT_AGENT_BADGE_PREFERENCES: AgentBadgePreferences = Object.freeze({
  version: 1,
  enabled: true,
  entries: Object.freeze({}),
});

function graphemes(value: string): string[] {
  if (typeof Intl !== "undefined" && "Segmenter" in Intl) {
    const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    return [...segmenter.segment(value)].map(({ segment }) => segment);
  }
  return Array.from(value);
}

function isWideGrapheme(value: string): boolean {
  return /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}\uFF01-\uFF60\uFFE0-\uFFE6]/u.test(
    value,
  );
}

/** Return a user-facing validation error, or null when the badge text fits. */
export function validateAgentBadgeLabel(value: string): string | null {
  const normalized = value.trim();
  if (!normalized) return "Enter badge text.";
  if (/[\p{C}\p{Z}]/u.test(normalized)) {
    return "Badge text cannot contain spaces or control characters.";
  }
  const parts = graphemes(normalized);
  if (parts.length > 2 || (parts.length === 2 && parts.some(isWideGrapheme))) {
    return "Use one wide symbol or up to two narrow characters.";
  }
  return null;
}

export function isAgentBadgeHexColor(value: unknown): value is string {
  return typeof value === "string" && HEX_COLOR.test(value);
}

export function isAgentBadgeTextColor(value: unknown): value is string {
  return value === "theme" || isAgentBadgeHexColor(value);
}

export function isAgentBadgeValue(value: unknown): value is AgentBadgeValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const raw = value as Partial<AgentBadgeValue>;
  return (
    typeof raw.label === "string" &&
    validateAgentBadgeLabel(raw.label) === null &&
    isAgentBadgeHexColor(raw.borderColor) &&
    isAgentBadgeTextColor(raw.textColor)
  );
}

function normalizeAgentBadgeValue(value: unknown): AgentBadgeValue | null {
  if (!isAgentBadgeValue(value)) return null;
  return {
    label: value.label.trim(),
    borderColor: value.borderColor.toLowerCase(),
    textColor: value.textColor.toLowerCase(),
  };
}

/** Sanitize persisted or imported data without allowing malformed rows into the UI. */
export function normalizeAgentBadgePreferences(value: unknown): AgentBadgePreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return DEFAULT_AGENT_BADGE_PREFERENCES;
  }
  const raw = value as { version?: unknown; enabled?: unknown; entries?: unknown };
  if (raw.version !== 1 || typeof raw.enabled !== "boolean") {
    return DEFAULT_AGENT_BADGE_PREFERENCES;
  }

  const normalizedEntries: [string, AgentBadgeValue][] = [];
  if (raw.entries && typeof raw.entries === "object" && !Array.isArray(raw.entries)) {
    for (const [agentId, candidate] of Object.entries(raw.entries).slice(0, MAX_AGENT_BADGES)) {
      if (!agentId || agentId.length > MAX_AGENT_ID_LENGTH) continue;
      const entry = normalizeAgentBadgeValue(candidate);
      if (entry) normalizedEntries.push([agentId, entry]);
    }
  }
  return { version: 1, enabled: raw.enabled, entries: Object.fromEntries(normalizedEntries) };
}

function isDefault(preferences: AgentBadgePreferences): boolean {
  return preferences.enabled && Object.keys(preferences.entries).length === 0;
}

let cachedRaw: string | null | undefined;
let cachedPreferences = DEFAULT_AGENT_BADGE_PREFERENCES;

export function readAgentBadgePreferences(): AgentBadgePreferences {
  if (typeof window === "undefined") return DEFAULT_AGENT_BADGE_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(AGENT_BADGE_STORAGE_KEY);
    if (raw === cachedRaw) return cachedPreferences;
    cachedRaw = raw;
    cachedPreferences = raw
      ? normalizeAgentBadgePreferences(JSON.parse(raw) as unknown)
      : DEFAULT_AGENT_BADGE_PREFERENCES;
    return cachedPreferences;
  } catch {
    cachedRaw = undefined;
    cachedPreferences = DEFAULT_AGENT_BADGE_PREFERENCES;
    return cachedPreferences;
  }
}

/** Persist the complete preference value and queue its account-scoped Server sync. */
export function writeAgentBadgePreferences(preferences: AgentBadgePreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeAgentBadgePreferences(preferences);
  try {
    if (isDefault(normalized)) window.localStorage.removeItem(AGENT_BADGE_STORAGE_KEY);
    else window.localStorage.setItem(AGENT_BADGE_STORAGE_KEY, JSON.stringify(normalized));
    cachedRaw = undefined;
    window.dispatchEvent(new Event(AGENT_BADGE_CHANGED_EVENT));
    queueUserPreferencePatch("agent_badges", isDefault(normalized) ? null : normalized);
  } catch {
    // A display preference must never break session navigation.
  }
}

export function agentBadgeFor(
  preferences: AgentBadgePreferences,
  agentId: string | null | undefined,
): AgentBadgeValue | null {
  if (!preferences.enabled || !agentId) return null;
  return Object.hasOwn(preferences.entries, agentId) ? preferences.entries[agentId] : null;
}
