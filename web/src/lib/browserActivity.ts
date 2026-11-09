import { useSyncExternalStore } from "react";

// Keep the line steady between consecutive tools, including when the pane remounts.
export const BROWSER_ACTIVITY_QUIET_MS = 2500;
const activity = new Map<string, { pending: number; timer?: ReturnType<typeof setTimeout> }>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

export function beginBrowserActivity(viewId: string): () => void {
  const current = activity.get(viewId) ?? { pending: 0 };
  clearTimeout(current.timer);
  current.pending += 1;
  activity.set(viewId, current);
  notify();
  let finished = false;
  return () => {
    if (finished) return;
    finished = true;
    current.pending -= 1;
    if (current.pending > 0) return;
    current.timer = setTimeout(() => {
      activity.delete(viewId);
      notify();
    }, BROWSER_ACTIVITY_QUIET_MS);
  };
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useBrowserActivity(viewId: string): boolean {
  return useSyncExternalStore(
    subscribe,
    () => activity.has(viewId),
    () => false,
  );
}
