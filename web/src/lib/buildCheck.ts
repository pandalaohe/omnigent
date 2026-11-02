// Adapted from omnigent-ae-public (Copyright 2026 Analytic Endeavors LLC, licensed under Apache-2.0).
//
// Detects that the server was redeployed while this tab kept running the page
// it loaded. The built index.html names its entry script with a content hash
// (`./assets/index-DRGsCGFe.js`); a deploy changes that name, so comparing the
// name this tab loaded with the one the server serves now tells whether the tab
// is stale. Pure functions are exported for tests.

import { getOmnigentHostConfig } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";

/** How often the server's start page is fetched while the tab is open. */
export const BUILD_CHECK_INTERVAL_MS = 2 * 60 * 1000;
/** Focus and visibility checks run at most this often. */
export const BUILD_CHECK_THROTTLE_MS = 30 * 1000;
/** A fetch that has not settled by then is aborted, so one stuck request never silences the check. */
export const BUILD_CHECK_TIMEOUT_MS = 15 * 1000;

const ENTRY_NAME = /(?:^|\/)(index-[A-Za-z0-9_-]+\.js)(?:[?#].*)?$/;

/** The entry script's file name when `src` is the built `index-HASH.js`, else null. */
export function entryNameFromSrc(src: string | null | undefined): string | null {
  if (!src) return null;
  const match = ENTRY_NAME.exec(src);
  return match ? match[1] : null;
}

/** The entry script name in a served index.html, or null (dev server, unknown shape). */
export function extractEntryName(html: string): string | null {
  const tags = html.match(/<script\b[^>]*>/gi) ?? [];
  for (const tag of tags) {
    if (!/\btype\s*=\s*["']module["']/i.test(tag)) continue;
    const src = /\bsrc\s*=\s*["']([^"']+)["']/i.exec(tag);
    const name = entryNameFromSrc(src ? src[1] : null);
    if (name) return name;
  }
  return null;
}

/** The entry script name of the page this tab is running. */
export function ownEntryName(doc: Document = document): string | null {
  const scripts = doc.querySelectorAll<HTMLScriptElement>('script[type="module"][src]');
  for (const script of Array.from(scripts)) {
    const name = entryNameFromSrc(script.getAttribute("src"));
    if (name) return name;
  }
  return null;
}

/** True when the server serves a different entry than the tab runs and this name was not announced yet. */
export function shouldNotify(
  own: string | null,
  served: string | null,
  announced: ReadonlySet<string>,
): boolean {
  return own !== null && served !== null && served !== own && !announced.has(served);
}

// Names already announced. Module scope, so a remount (the shell re-rendering
// under a new route tree) never shows the same build's toast again after Later.
const announced = new Set<string>();

/** Forgets every announced name. Tests only. */
export function resetAnnouncedBuildsForTests(): void {
  announced.clear();
}

/**
 * Starts the checks; returns a cleanup. `onNewBuild` fires once per new name.
 * A failed fetch, a non-200 answer or a page without an entry name never fires.
 * Nothing starts when a host fetcher is installed (embedded or desktop): the
 * host owns the document and the network path there.
 */
export function startBuildCheck(onNewBuild: (name: string) => void): () => void {
  if (getOmnigentHostConfig().fetcher) return () => {};
  const own = ownEntryName();
  if (own === null) return () => {};
  let lastCheck = Number.NEGATIVE_INFINITY;
  let inFlight = false;
  let stopped = false;

  const check = async () => {
    if (inFlight) return;
    inFlight = true;
    lastCheck = Date.now();
    // A controller and setTimeout rather than AbortSignal.timeout, so fake timers can drive it.
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), BUILD_CHECK_TIMEOUT_MS);
    try {
      const response = await authenticatedFetch("/", {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok || stopped) return;
      const served = extractEntryName(await response.text());
      if (stopped || served === null || !shouldNotify(own, served, announced)) return;
      announced.add(served);
      onNewBuild(served);
    } catch {
      // Offline or the server is restarting: try again next time.
    } finally {
      clearTimeout(abortTimer);
      inFlight = false;
    }
  };

  const throttled = () => {
    if (document.visibilityState === "hidden") return;
    if (Date.now() - lastCheck >= BUILD_CHECK_THROTTLE_MS) void check();
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") throttled();
  };

  const timer = setInterval(throttled, BUILD_CHECK_INTERVAL_MS);
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("focus", throttled);
  return () => {
    stopped = true;
    clearInterval(timer);
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("focus", throttled);
  };
}
