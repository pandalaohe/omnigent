import { queueUserPreferencePatch } from "./userPreferencesSync";

export const SESSION_NAVIGATION_STORAGE_KEY = "omnigent:session-navigation";
export const SESSION_NAVIGATION_CHANGED_EVENT = "omnigent:session-navigation-changed";

export const MAX_SESSION_POLLING_WINDOW_HOURS = 24 * 365;

export type NativeMobileHeaderMode = "server" | "conversation-title";

export interface SessionNavigationPreferences {
  /** Null keeps the official behavior: every non-archived session participates. */
  pollingActiveWindowHours: number | null;
  /** Native iOS defaults to its official top-of-screen Server switcher. */
  nativeMobileHeaderMode: NativeMobileHeaderMode;
}

const DEFAULT_PREFERENCES: SessionNavigationPreferences = {
  pollingActiveWindowHours: null,
  nativeMobileHeaderMode: "server",
};

function normalizePollingWindow(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const hours = Math.round(value);
  if (hours < 1) return null;
  return Math.min(hours, MAX_SESSION_POLLING_WINDOW_HOURS);
}

function normalizePreferences(value: unknown): SessionNavigationPreferences {
  if (!value || typeof value !== "object") return { ...DEFAULT_PREFERENCES };
  const candidate = value as Partial<SessionNavigationPreferences>;
  return {
    pollingActiveWindowHours: normalizePollingWindow(candidate.pollingActiveWindowHours),
    nativeMobileHeaderMode:
      candidate.nativeMobileHeaderMode === "conversation-title" ? "conversation-title" : "server",
  };
}

export function readSessionNavigationPreferences(): SessionNavigationPreferences {
  if (typeof window === "undefined") return { ...DEFAULT_PREFERENCES };
  try {
    const raw = window.localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY);
    return raw ? normalizePreferences(JSON.parse(raw)) : { ...DEFAULT_PREFERENCES };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function writeSessionNavigationPreferences(preferences: SessionNavigationPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizePreferences(preferences);
  try {
    if (
      normalized.pollingActiveWindowHours === null &&
      normalized.nativeMobileHeaderMode === "server"
    ) {
      window.localStorage.removeItem(SESSION_NAVIGATION_STORAGE_KEY);
    } else {
      window.localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify(normalized));
    }
  } catch {
    // Storage denial or quota exhaustion must not break navigation.
  }
  window.dispatchEvent(new Event(SESSION_NAVIGATION_CHANGED_EVENT));
  queueUserPreferencePatch(
    "session_navigation",
    normalized.pollingActiveWindowHours === null && normalized.nativeMobileHeaderMode === "server"
      ? null
      : normalized,
  );
}

/**
 * Whether a session belongs to the optional active polling window.
 * `updated_at` is the list endpoint's Unix-seconds activity/sort timestamp.
 */
export function isSessionInsidePollingWindow(
  updatedAtSeconds: number,
  activeWindowHours: number | null,
  nowMs = Date.now(),
): boolean {
  if (activeWindowHours === null) return true;
  if (!Number.isFinite(updatedAtSeconds)) return false;
  return updatedAtSeconds >= Math.floor(nowMs / 1000) - activeWindowHours * 60 * 60;
}

/** Native Server stays on the chat by default; title mode moves it to the open sidebar. */
export function shouldHideNativeServerSwitcher({
  frontmost,
  sidebarOpen,
  headerMode,
}: {
  frontmost: boolean;
  sidebarOpen: boolean;
  headerMode: NativeMobileHeaderMode;
}): boolean {
  if (headerMode === "server") return !frontmost;
  return !sidebarOpen;
}
