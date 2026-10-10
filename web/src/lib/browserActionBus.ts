/**
 * Tiny fan-out bus for `browser.action_request` SSE events, bridging the store
 * (`handleSessionEvent`) to the relay hook (`useBrowserAgentRelay`). Its own
 * module so neither imports the other (avoids a cycle). Set-based so Strict-Mode
 * double-mounts dedupe; no-op when nothing is registered (plain-browser renderers).
 */
import type { BrowserActionRequestEvent } from "./events";

/**
 * `conversationId` is the conversation whose STREAM delivered the action — the
 * session the relay must claim/dispatch/post against. The event payload carries
 * no conversation id, and with background streams the delivering conversation
 * may not be the one the relay was mounted for, so it has to ride alongside.
 */
export type BrowserActionListener = (
  event: BrowserActionRequestEvent,
  conversationId: string | null,
) => void;

const listeners = new Set<BrowserActionListener>();
type BrowserActionClaimedListener = (conversationId: string, tabId: string) => void;
const claimedListeners = new Set<BrowserActionClaimedListener>();

/** Actions that need the owning session's browser pane surfaced: navigate
 *  loads it, screenshot can only capture a view that is on screen. */
export function surfacesBrowserPane(action: string): boolean {
  return action === "navigate" || action === "screenshot";
}

/** Subscribe to browser action requests; returns an unsubscribe. */
export function onBrowserActionRequest(listener: BrowserActionListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Fan a browser action request out to every listener; a throwing listener is
 *  isolated so it can't stop the others or the event pump. */
export function emitBrowserActionRequest(
  event: BrowserActionRequestEvent,
  conversationId: string | null,
): void {
  for (const listener of listeners) {
    try {
      listener(event, conversationId);
    } catch (err) {
      console.warn("[browser-relay] action listener threw:", err);
    }
  }
}

/** Surface only a validated target after this renderer wins the action claim. */
export function onBrowserActionClaimed(listener: BrowserActionClaimedListener): () => void {
  claimedListeners.add(listener);
  return () => claimedListeners.delete(listener);
}

export function emitBrowserActionClaimed(conversationId: string, tabId: string): void {
  for (const listener of claimedListeners) {
    try {
      listener(conversationId, tabId);
    } catch (err) {
      console.warn("[browser-relay] claimed action listener threw:", err);
    }
  }
}
