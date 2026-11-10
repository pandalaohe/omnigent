/**
 * Tiny fan-out bus for `artifact.open_request` SSE events, bridging the store
 * (`handleSessionEvent`) to the shell's viewer opener. Its own module so
 * neither imports the other (avoids a cycle). Set-based so Strict-Mode
 * double-mounts dedupe; pending requests are retained per session.
 */
import type { ArtifactOpenRequestEvent } from "./events";

/**
 * `conversationId` is the conversation whose STREAM delivered the request — the
 * session the UI may open against. The event payload carries no conversation
 * id, and with background streams the delivering conversation may not be the
 * one on screen, so it has to ride alongside (same as `browserActionBus`).
 */
export type ArtifactOpenListener = (
  event: ArtifactOpenRequestEvent,
  conversationId: string | null,
) => void;

const listeners = new Map<string, Set<ArtifactOpenListener>>();
const pending = new Map<string, ArtifactOpenRequestEvent>();

/** Forget a deferred request when its session is archived, deleted or closed. */
export function clearArtifactOpenRequest(conversationId: string): void {
  pending.delete(conversationId);
}

/** Subscribe to panel-open requests; returns an unsubscribe. */
export function onArtifactOpenRequest(
  conversationId: string,
  listener: ArtifactOpenListener,
): () => void {
  const sessionListeners = listeners.get(conversationId) ?? new Set<ArtifactOpenListener>();
  listeners.set(conversationId, sessionListeners);
  sessionListeners.add(listener);
  const event = pending.get(conversationId);
  if (event) {
    pending.delete(conversationId);
    listener(event, conversationId);
  }
  return () => {
    sessionListeners.delete(listener);
    if (!sessionListeners.size) listeners.delete(conversationId);
  };
}

/** Fan a panel-open request out to every listener; a throwing listener is
 *  isolated so it can't stop the others or the event pump. */
export function emitArtifactOpenRequest(
  event: ArtifactOpenRequestEvent,
  conversationId: string | null,
): void {
  if (!conversationId) return;
  const sessionListeners = listeners.get(conversationId);
  if (!sessionListeners?.size) {
    pending.set(conversationId, event);
    return;
  }
  for (const listener of sessionListeners) {
    try {
      listener(event, conversationId);
    } catch (err) {
      console.warn("[artifact-open] listener threw:", err);
    }
  }
}
