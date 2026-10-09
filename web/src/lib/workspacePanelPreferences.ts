// Persisted, app-global preference for whether a brand-new chat's right
// Workspace rail (Files / Agents / Shells) starts open or collapsed.
//
// Set from Appearance settings, and re-written whenever the user collapses or
// expands the rail — so the state the rail was last left in carries into the
// next chat instead of springing back open.
//
// This only seeds sessions that have no saved per-chat `open` state. Once a
// user toggles the rail in a session, that session's own
// `SessionWorkspaceState.open` wins on restore.

const STORAGE_KEY = "omnigent:default-workspace-panel";

export const workspacePanelDefaults = ["open", "collapsed"] as const;
export type WorkspacePanelDefault = (typeof workspacePanelDefaults)[number];

/** Keep new chats visually stable while their runner and workspace hydrate. */
export const WORKSPACE_PANEL_DEFAULT: WorkspacePanelDefault = "collapsed";

/** Return whether a string is one of the selectable Workspace panel defaults. */
export function isWorkspacePanelDefault(
  value: string | null | undefined,
): value is WorkspacePanelDefault {
  return value === "open" || value === "collapsed";
}

/**
 * Normalize a stored Workspace panel default to the product default.
 *
 * Unknown values can only come from localStorage drift or manual edits.
 * Falling back to the product default keeps unknown values deterministic.
 */
export function normalizeWorkspacePanelDefault(
  value: string | null | undefined,
): WorkspacePanelDefault {
  return isWorkspacePanelDefault(value) ? value : WORKSPACE_PANEL_DEFAULT;
}

/**
 * Read the persisted default for new-chat Workspace rail visibility.
 *
 * Returns the product default when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/unknown — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readWorkspacePanelDefault(): WorkspacePanelDefault {
  if (typeof window === "undefined") return WORKSPACE_PANEL_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return WORKSPACE_PANEL_DEFAULT;
    return normalizeWorkspacePanelDefault(raw);
  } catch {
    return WORKSPACE_PANEL_DEFAULT;
  }
}

/**
 * Persist the default Workspace panel visibility for new chats. The product
 * default clears the key. Swallows quota/access errors so a failed
 * write can't break settings.
 */
export function writeWorkspacePanelDefault(value: WorkspacePanelDefault): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeWorkspacePanelDefault(value);
    if (normalized === WORKSPACE_PANEL_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}

/**
 * Boolean form of {@link readWorkspacePanelDefault} for AppShell's
 * `rightPanelOpen` fallback when a session has no saved `open` state.
 */
export function readDefaultWorkspacePanelOpen(): boolean {
  return readWorkspacePanelDefault() === "open";
}

/**
 * Record the rail's visibility as the app-global default.
 *
 * Called from AppShell's collapse/expand toggle so the state the user left the
 * rail in becomes the starting state for chats they haven't opened yet.
 */
export function writeDefaultWorkspacePanelOpen(open: boolean): void {
  writeWorkspacePanelDefault(open ? "open" : "collapsed");
}

const WIDEN_FOR_CONTENT_KEY = "omnigent:workspace-panel-widen-for-content";
export const WIDEN_FOR_CONTENT_CHANGED_EVENT = "omnigent:workspace-panel-widen-for-content-changed";

/** Whether the rail uses its own width while it shows a browser tab or an opened file. On by default. */
export function readWidenWorkspaceForContent(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(WIDEN_FOR_CONTENT_KEY) !== "0";
  } catch {
    return true;
  }
}

/** Persist whether the rail uses its own width for browser/file content. The default clears the key. */
export function writeWidenWorkspaceForContent(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled) {
      window.localStorage.removeItem(WIDEN_FOR_CONTENT_KEY);
    } else {
      window.localStorage.setItem(WIDEN_FOR_CONTENT_KEY, "0");
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
  window.dispatchEvent(new Event(WIDEN_FOR_CONTENT_CHANGED_EVENT));
}

/** Subscribe to widen-for-content changes in this tab and from other tabs. */
export function subscribeWidenWorkspaceForContent(callback: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(WIDEN_FOR_CONTENT_CHANGED_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(WIDEN_FOR_CONTENT_CHANGED_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
}
