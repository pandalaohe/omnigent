// Adapted from omnigent-ae-public (Copyright 2026 Analytic Endeavors LLC, licensed under Apache-2.0).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BUILD_CHECK_INTERVAL_MS,
  BUILD_CHECK_THROTTLE_MS,
  BUILD_CHECK_TIMEOUT_MS,
  entryNameFromSrc,
  extractEntryName,
  ownEntryName,
  resetAnnouncedBuildsForTests,
  shouldNotify,
  startBuildCheck,
} from "./buildCheck";

const hostState = vi.hoisted(() => ({ config: {} as { fetcher?: unknown } }));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getOmnigentHostConfig: () => hostState.config,
}));

// The head of a real built index.html: relative src, crossorigin.
const BUILT = `<!doctype html><html><head>
<link rel="icon" href="./favicon.svg">
<script type="module" crossorigin src="./assets/index-DRGsCGFe.js"></script>
<link rel="modulepreload" crossorigin href="./assets/rolldown-runtime-CNC7AqOf.js">
</head><body><div id="root"></div></body></html>`;

const page = (name: string) => BUILT.replace("index-DRGsCGFe.js", name);

describe("extractEntryName", () => {
  it("reads the entry from a real built index.html", () => {
    expect(extractEntryName(BUILT)).toBe("index-DRGsCGFe.js");
  });
  it("reads an absolute src", () => {
    expect(extractEntryName('<script type="module" src="/assets/index-AbC_1-x.js"></script>')).toBe(
      "index-AbC_1-x.js",
    );
  });
  it("returns null for the dev page, a modulepreload and plain text", () => {
    expect(extractEntryName('<script type="module" src="/src/main.tsx"></script>')).toBeNull();
    expect(extractEntryName('<link rel="modulepreload" href="./assets/index-X.js">')).toBeNull();
    expect(extractEntryName("")).toBeNull();
  });
  it("ignores a chunk that is not the index entry", () => {
    expect(entryNameFromSrc("./assets/shiki-Yl5JeILm.js")).toBeNull();
    expect(entryNameFromSrc(null)).toBeNull();
  });
});

describe("shouldNotify", () => {
  it("needs both names, a difference and an unannounced name", () => {
    expect(shouldNotify("a.js", "b.js", new Set())).toBe(true);
    expect(shouldNotify("a.js", "a.js", new Set())).toBe(false);
    expect(shouldNotify(null, "b.js", new Set())).toBe(false);
    expect(shouldNotify("a.js", null, new Set())).toBe(false);
    expect(shouldNotify("a.js", "b.js", new Set(["b.js"]))).toBe(false);
  });
});

describe("startBuildCheck", () => {
  const fetchMock = vi.fn();
  const served = (html: string, ok = true) =>
    fetchMock.mockResolvedValue({ ok, text: async () => html });
  const tick = async (ms: number) => {
    await vi.advanceTimersByTimeAsync(ms);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    resetAnnouncedBuildsForTests();
    hostState.config = {};
    vi.stubGlobal("fetch", fetchMock);
    const script = document.createElement("script");
    script.type = "module";
    script.setAttribute("src", "./assets/index-DRGsCGFe.js");
    document.head.appendChild(script);
  });
  afterEach(() => {
    document.head.querySelectorAll("script").forEach((s) => s.remove());
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("finds the page's own entry in the DOM", () => {
    expect(ownEntryName()).toBe("index-DRGsCGFe.js");
  });

  it("does not fire for the same name", async () => {
    served(BUILT);
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(cb).not.toHaveBeenCalled();
    stop();
  });

  it("fires once per new name", async () => {
    served(page("index-NEW1.js"));
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    await tick(BUILD_CHECK_INTERVAL_MS * 3);
    expect(cb).toHaveBeenCalledTimes(1);
    expect(cb).toHaveBeenCalledWith("index-NEW1.js");
    served(page("index-NEW2.js"));
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(cb).toHaveBeenCalledTimes(2);
    stop();
  });

  it("never fires on a failed fetch, a non-200 answer or a missing entry", async () => {
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    fetchMock.mockRejectedValue(new Error("offline"));
    await tick(BUILD_CHECK_INTERVAL_MS);
    served(page("index-NEW1.js"), false);
    await tick(BUILD_CHECK_INTERVAL_MS);
    served('<script type="module" src="/src/main.tsx"></script>');
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(cb).not.toHaveBeenCalled();
    stop();
  });

  it("checks on focus, throttled to once per 30 seconds", async () => {
    served(BUILT);
    const stop = startBuildCheck(vi.fn());
    window.dispatchEvent(new Event("focus"));
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("focus"));
    await tick(1000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await tick(BUILD_CHECK_THROTTLE_MS);
    document.dispatchEvent(new Event("visibilitychange"));
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    stop();
  });

  it("does not check without an entry name in the page (dev server)", async () => {
    document.head.querySelectorAll("script").forEach((s) => s.remove());
    served(page("index-NEW1.js"));
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).not.toHaveBeenCalled();
    stop();
  });

  it("stops its timers and listeners on cleanup", async () => {
    served(page("index-NEW1.js"));
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    stop();
    window.dispatchEvent(new Event("focus"));
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(cb).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("starts nothing when a host fetcher is installed (embedded or desktop)", async () => {
    hostState.config = { fetcher: fetchMock };
    served(page("index-NEW1.js"));
    const cb = vi.fn();
    const stop = startBuildCheck(cb);
    window.dispatchEvent(new Event("focus"));
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(cb).not.toHaveBeenCalled();
    stop();
    hostState.config = {};
  });

  it("skips ticks while the tab is hidden and fetches again once visible", async () => {
    served(BUILT);
    const vis = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const stop = startBuildCheck(vi.fn());
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(fetchMock).not.toHaveBeenCalled();
    vis.mockReturnValue("visible");
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    stop();
    vis.mockRestore();
  });

  it("never fetches twice at once for a tick and a focus event", async () => {
    let release: (v: unknown) => void = () => {};
    fetchMock.mockReturnValue(
      new Promise((r) => {
        release = r;
      }),
    );
    const stop = startBuildCheck(vi.fn());
    await tick(BUILD_CHECK_INTERVAL_MS);
    window.dispatchEvent(new Event("focus"));
    await tick(BUILD_CHECK_THROTTLE_MS);
    window.dispatchEvent(new Event("focus"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release({ ok: true, text: async () => BUILT });
    await tick(0);
    stop();
  });

  it("routes the interval tick through the throttle", async () => {
    served(BUILT);
    const stop = startBuildCheck(vi.fn());
    window.dispatchEvent(new Event("focus"));
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    stop();
  });

  it("aborts a fetch that never settles and fetches again on the next tick", async () => {
    let signal: AbortSignal | undefined;
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          signal = init.signal ?? undefined;
          signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const stop = startBuildCheck(vi.fn());
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signal?.aborted).toBe(false);
    await tick(BUILD_CHECK_TIMEOUT_MS);
    expect(signal?.aborted).toBe(true);
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    stop();
  });
});
