import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue, assertStable, captureCrop, cropBounds, matchesSender } from "../src/capture";
import { activation, metrics, selection } from "./fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("measured bitmap crop mapping", () => {
  it("compares values without treating wire key order as page motion", () => {
    expect(() =>
      assertStable(selection, {
        ...selection,
        metrics: {
          ...metrics,
          url: metrics.url,
          visual: { scale: 1, height: 800, width: 1000, y: 0, x: 0 },
        },
      }),
    ).not.toThrow();
  });
  it("uses independent actual x/y ratios, clips, and rounds outwards, without multiplying DPR", () => {
    expect(cropBounds(selection.rect, metrics, 1500, 1200)).toEqual({
      left: 15,
      top: 30,
      width: 31,
      height: 16,
      scale: { x: 1.5, y: 1.5 },
    });
    expect(cropBounds({ x: -10, y: 790, width: 30, height: 30 }, metrics, 1500, 1199)).toEqual({
      left: 0,
      top: 1184,
      width: 30,
      height: 15,
      scale: { x: 1.5, y: 1.49875 },
    });
    expect(cropBounds({ x: 999.2, y: 799.2, width: 5, height: 5 }, metrics, 2000, 1600)).toEqual({
      left: 1998,
      top: 1598,
      width: 2,
      height: 2,
      scale: { x: 2, y: 2 },
    });
  });
  it.each([
    { clientWidth: 985, clientHeight: 800 },
    { clientWidth: 1000, clientHeight: 785 },
    { clientWidth: 985, clientHeight: 785 },
  ])("accepts measured classic scrollbars without changing bitmap denominators: %o", (client) => {
    const scrollbars = {
      ...metrics,
      ...client,
      visual: { ...metrics.visual, width: client.clientWidth, height: client.clientHeight },
    };
    expect(cropBounds(selection.rect, scrollbars, 1500, 1200)).toEqual({
      left: 15,
      top: 30,
      width: 31,
      height: 16,
      scale: { x: 1.5, y: 1.5 },
    });
    expect(() =>
      cropBounds(
        selection.rect,
        { ...scrollbars, visual: { ...scrollbars.visual, width: client.clientWidth - 10 } },
        1500,
        1200,
      ),
    ).toThrow("dimensions");
  });
  it.each([
    { ...metrics, visual: { ...metrics.visual, scale: 1.25 } },
    { ...metrics, visual: { ...metrics.visual, x: 2 } },
    { ...metrics, visual: { ...metrics.visual, y: 1 } },
  ])("rejects pinch zoom rather than guessing", (value) => {
    expect(() => cropBounds(selection.rect, value, 1500, 1200)).toThrow("Reset pinch zoom");
  });
  it("rejects impossible metrics, hidden pages, empty clips and inconsistent scales", () => {
    expect(() => cropBounds(selection.rect, metrics, 1500, 1100)).toThrow("disagree");
    expect(() => cropBounds(selection.rect, { ...metrics, visible: false }, 1500, 1200)).toThrow(
      "dimensions",
    );
    expect(() => cropBounds(selection.rect, metrics, NaN, 1200)).toThrow("dimensions");
    expect(() => cropBounds({ ...selection.rect, x: 1200 }, metrics, 1500, 1200)).toThrow(
      "outside",
    );
  });
  it.each([
    "scrollX",
    "scrollY",
    "dpr",
    "width",
    "height",
    "clientWidth",
    "clientHeight",
    "revision",
  ] as const)("detects changed %s", (key) => {
    expect(() =>
      assertStable(selection, {
        ...selection,
        metrics: { ...metrics, [key]: metrics[key] + 1 },
      }),
    ).toThrow("changed");
  });
  it("detects target/text/page changes", () => {
    expect(() =>
      assertStable(selection, {
        ...selection,
        target: { ...selection.target, quote: "Changed" },
      }),
    ).toThrow("changed");
    expect(() =>
      assertStable(selection, {
        ...selection,
        rect: { ...selection.rect, x: 12 },
      }),
    ).toThrow("changed");
    expect(() =>
      assertStable(selection, {
        ...selection,
        page: { ...selection.page, url: "https://other.test/" },
      }),
    ).toThrow("changed");
  });
});

describe("worker capture boundary", () => {
  const calls: string[] = [];
  const query = vi.fn();
  const getWindow = vi.fn();
  const screenshot = vi.fn();
  const send = vi.fn();
  beforeEach(() => {
    calls.length = 0;
    query.mockReset().mockResolvedValue([{ id: 7, windowId: 3, active: true }]);
    getWindow.mockReset().mockResolvedValue({ focused: true });
    screenshot.mockReset().mockImplementation(async () => {
      calls.push("screenshot");
      return "data:image/png;base64,AA==";
    });
    send.mockReset().mockImplementation(async (_tab, message) => {
      calls.push(message.type);
      return selection;
    });
    vi.stubGlobal("chrome", {
      runtime: { id: "extension" },
      tabs: { query, captureVisibleTab: screenshot, sendMessage: send },
      windows: { get: getWindow, update: vi.fn().mockResolvedValue({}) },
    });
  });
  it("requires exact extension/tab/window/frame/document/generation binding", () => {
    const sender = {
      id: "extension",
      tab: { id: 7, windowId: 3, active: true },
      frameId: 0,
      documentId: "document-a",
    };
    expect(matchesSender(activation, sender, "generation-a")).toBe(true);
    for (const bad of [
      { ...sender, frameId: 1 },
      { ...sender, id: "other" },
      { ...sender, documentId: "document-b" },
      { ...sender, tab: { id: 8, windowId: 3, active: true } },
      { ...sender, tab: { id: 7, windowId: 4, active: true } },
    ])
      expect(matchesSender(activation, bad, "generation-a")).toBe(false);
    expect(matchesSender(activation, sender, "stale")).toBe(false);
  });
  it("serializes capture attempts and spaces actual API calls by 600ms after slow preparation", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const times: number[] = [];
    screenshot.mockImplementation(async () => {
      times.push(Date.now());
      return "x";
    });
    const queue = new CaptureQueue();
    const first = queue.run(async () => {
      await new Promise((resolve) => setTimeout(resolve, 550));
      return queue.visibleTab(3, async () => undefined);
    });
    const second = queue.run(() => queue.visibleTab(3, async () => undefined));
    await vi.runAllTimersAsync();
    await Promise.all([first, second]);
    expect(times).toEqual([550, 1150]);
  });
  it("fails visibly without automatic retry and always restores overlays", async () => {
    screenshot.mockRejectedValueOnce(new Error("Capture denied"));
    await expect(captureCrop({ ...activation }, selection, new CaptureQueue())).rejects.toThrow(
      "Capture denied",
    );
    expect(screenshot).toHaveBeenCalledTimes(1);
    expect(calls).toEqual(["prepare-capture", "restore-overlay"]);
  });
  it("rejects source tab/focus changes before taking a screenshot", async () => {
    query.mockResolvedValue([{ id: 8 }]);
    await expect(captureCrop({ ...activation }, selection, new CaptureQueue())).rejects.toThrow(
      "Source tab",
    );
    expect(screenshot).not.toHaveBeenCalled();
    query.mockResolvedValue([{ id: 7 }]);
    getWindow.mockResolvedValue({ focused: false });
    await expect(captureCrop({ ...activation }, selection, new CaptureQueue())).rejects.toThrow(
      "Source tab",
    );
  });
  it("checks again after the rate-limit wait and rejects invalidated capture", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const queue = new CaptureQueue();
    await queue.visibleTab(3, async () => undefined);
    const current = { ...activation };
    const pending = captureCrop(current, selection, queue);
    const assertion = expect(pending).rejects.toThrow("Source tab");
    await vi.advanceTimersByTimeAsync(100);
    current.valid = false;
    await vi.runAllTimersAsync();
    await assertion;
    expect(screenshot).toHaveBeenCalledTimes(1);
    expect(calls.at(-1)).toBe("restore-overlay");
  });
  it("rejects a post-screenshot document/scroll/geometry change", async () => {
    send.mockImplementation(async (_tab, message) => {
      calls.push(message.type);
      return message.type === "measure-capture"
        ? { ...selection, metrics: { ...metrics, revision: 1 } }
        : selection;
    });
    await expect(captureCrop({ ...activation }, selection, new CaptureQueue())).rejects.toThrow(
      "changed",
    );
    expect(calls).toEqual(["prepare-capture", "screenshot", "measure-capture", "restore-overlay"]);
  });
  it("produces a crop with a checked canvas and closes the decoded bitmap", async () => {
    const close = vi.fn();
    const draw = vi.fn();
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockResolvedValue({ width: 1500, height: 1200, close }),
    );
    vi.stubGlobal(
      "OffscreenCanvas",
      class {
        getContext() {
          return { drawImage: draw };
        }
        async convertToBlob() {
          return new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
        }
      },
    );
    const result = await captureCrop({ ...activation }, selection, new CaptureQueue());
    expect(result.image).toBe("data:image/png;base64,AQID");
    expect(result.geometry.bitmap_scale).toEqual({ x: 1.5, y: 1.5 });
    expect(draw.mock.calls[0]?.slice(1)).toEqual([15, 30, 31, 16, 0, 0, 31, 16]);
    expect(close).toHaveBeenCalledOnce();
    expect(calls.at(-1)).toBe("restore-overlay");
  });
});
