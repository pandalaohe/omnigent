/**
 * Tiny fan-out bus for `artifact.open_request` SSE events, bridging the store
 * (`handleSessionEvent`) to the shell's viewer opener. Its own module so
 * neither imports the other (avoids a cycle). Set-based so Strict-Mode
 * double-mounts dedupe; no-op when nothing is registered.
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

const listeners = new Set<ArtifactOpenListener>();

/** Subscribe to panel-open requests; returns an unsubscribe. */
export function onArtifactOpenRequest(listener: ArtifactOpenListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Fan a panel-open request out to every listener; a throwing listener is
 *  isolated so it can't stop the others or the event pump. */
export function emitArtifactOpenRequest(
  event: ArtifactOpenRequestEvent,
  conversationId: string | null,
): void {
  for (const listener of listeners) {
    try {
      listener(event, conversationId);
    } catch (err) {
      console.warn("[artifact-open] listener threw:", err);
    }
  }
}
