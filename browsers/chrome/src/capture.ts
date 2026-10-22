import { parseSelection } from "./protocol";
import type { Activation, ItemV1, Metrics, Rect, Selection } from "./protocol";

export function assertStable(before: Selection, after: Selection): void {
  const same = (left: object, right: object) =>
    Object.keys(left).length === Object.keys(right).length &&
    Object.entries(left).every(([key, value]) => (right as Record<string, unknown>)[key] === value);
  const { visual: beforeVisual, ...beforeMetrics } = before.metrics;
  const { visual: afterVisual, ...afterMetrics } = after.metrics;
  if (
    before.kind !== after.kind ||
    !same(before.page, after.page) ||
    !same(before.target, after.target) ||
    !same(before.rect, after.rect) ||
    !same(beforeMetrics, afterMetrics) ||
    !same(beforeVisual, afterVisual) ||
    !before.metrics.visible ||
    !after.metrics.visible
  ) {
    throw new Error("Page changed during capture. Select again.");
  }
}

export function cropBounds(rect: Rect, metrics: Metrics, width: number, height: number) {
  const visual = metrics.visual;
  if (visual.scale !== 1 || visual.x !== 0 || visual.y !== 0) {
    throw new Error("Reset pinch zoom and capture again.");
  }
  if (
    !metrics.visible ||
    ![
      width,
      height,
      metrics.width,
      metrics.height,
      metrics.clientWidth,
      metrics.clientHeight,
      metrics.dpr,
      rect.x,
      rect.y,
      rect.width,
      rect.height,
      visual.width,
      visual.height,
    ].every(Number.isFinite) ||
    width <= 0 ||
    height <= 0 ||
    !Number.isInteger(width) ||
    !Number.isInteger(height) ||
    width * height > 16000000 ||
    metrics.width <= 0 ||
    metrics.height <= 0 ||
    metrics.clientWidth <= 0 ||
    metrics.clientHeight <= 0 ||
    metrics.clientWidth > metrics.width ||
    metrics.clientHeight > metrics.height ||
    metrics.dpr <= 0 ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    Math.abs(visual.width - metrics.clientWidth) > 2 ||
    Math.abs(visual.height - metrics.clientHeight) > 2
  )
    throw new Error("Invalid screenshot dimensions");
  const x = width / metrics.width;
  const y = height / metrics.height;
  // Bitmap ratios include Chrome zoom and OS scaling; DPR must not be applied twice.
  if (
    x > 8 ||
    y > 8 ||
    x < 0.25 ||
    y < 0.25 ||
    Math.abs(x - y) > Math.max(2 / metrics.width, 2 / metrics.height)
  ) {
    throw new Error("Screenshot and viewport dimensions disagree");
  }
  const left = Math.max(0, Math.floor(rect.x * x));
  const top = Math.max(0, Math.floor(rect.y * y));
  const right = Math.min(width, Math.ceil((rect.x + rect.width) * x));
  const bottom = Math.min(height, Math.ceil((rect.y + rect.height) * y));
  if (right <= left || bottom <= top) throw new Error("Selection is outside the visible viewport");
  return {
    left,
    top,
    width: right - left,
    height: bottom - top,
    scale: { x, y },
  };
}

/** Queue failures never poison later explicit captures; no automatic retries. */
export class CaptureQueue {
  private pending: Promise<unknown> = Promise.resolve();
  private lastStart = -Infinity;

  run<T>(capture: () => Promise<T>): Promise<T> {
    const result = this.pending.then(capture);
    this.pending = result.catch(() => undefined);
    return result;
  }

  async visibleTab(windowId: number, checkSource: () => Promise<void>): Promise<string> {
    const delay = Math.max(0, this.lastStart + 600 - Date.now());
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    await checkSource();
    this.lastStart = Date.now();
    return chrome.tabs.captureVisibleTab(windowId, { format: "png" });
  }
}

export function matchesSender(
  activation: Activation,
  sender: chrome.runtime.MessageSender,
  generation: unknown,
): boolean {
  return (
    activation.valid &&
    sender.id === chrome.runtime.id &&
    sender.frameId === 0 &&
    sender.tab?.id === activation.tabId &&
    sender.tab?.windowId === activation.windowId &&
    sender.documentId === activation.documentId &&
    generation === activation.generation
  );
}

async function assertSource(activation: Activation): Promise<void> {
  const [tabs, window] = await Promise.all([
    chrome.tabs.query({ active: true, windowId: activation.windowId }),
    chrome.windows.get(activation.windowId),
  ]);
  if (
    !activation.valid ||
    tabs.length !== 1 ||
    tabs[0]?.id !== activation.tabId ||
    !window.focused
  ) {
    throw new Error("Source tab or window changed. Select again.");
  }
}

export async function captureCrop(
  activation: Activation,
  selection: Selection,
  queue: CaptureQueue,
): Promise<{ image: string; geometry: ItemV1["geometry"] }> {
  const send = async (type: string) =>
    chrome.tabs.sendMessage(
      activation.tabId,
      { type, generation: activation.generation },
      { documentId: activation.documentId },
    );
  try {
    await chrome.windows.update(activation.windowId, { focused: true });
    await assertSource(activation);
    const before = parseSelection(await send("prepare-capture"));
    assertStable(selection, before);
    cropBounds(
      before.rect,
      before.metrics,
      Math.round(before.metrics.width),
      Math.round(before.metrics.height),
    );
    await assertSource(activation);
    const image = await queue.visibleTab(activation.windowId, () => assertSource(activation));
    await assertSource(activation);
    const after = parseSelection(await send("measure-capture"));
    assertStable(before, after);
    if (!activation.valid) throw new Error("Capture was invalidated. Select again.");
    if (!image.startsWith("data:image/png;base64,"))
      throw new Error("Chrome returned an invalid screenshot");
    const raw = atob(image.slice("data:image/png;base64,".length));
    const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    try {
      const bounds = cropBounds(before.rect, before.metrics, bitmap.width, bitmap.height);
      const canvas = new OffscreenCanvas(bounds.width, bounds.height);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Screenshot cropping is unavailable");
      context.drawImage(
        bitmap,
        bounds.left,
        bounds.top,
        bounds.width,
        bounds.height,
        0,
        0,
        bounds.width,
        bounds.height,
      );
      const crop = await canvas.convertToBlob({ type: "image/png" });
      if (!crop.size || crop.size > 2 * 1024 * 1024)
        throw new Error("Cropped screenshot exceeds 2 MiB. Select a smaller region.");
      const buffer = new Uint8Array(await crop.arrayBuffer());
      let base64 = "";
      for (let offset = 0; offset < buffer.length; offset += 8192)
        base64 += String.fromCharCode(...buffer.subarray(offset, offset + 8192));
      if (!activation.valid) throw new Error("Capture was invalidated. Select again.");
      return {
        image: `data:image/png;base64,${btoa(base64)}`,
        geometry: {
          rect: before.rect,
          viewport: {
            width: before.metrics.width,
            height: before.metrics.height,
          },
          bitmap_scale: bounds.scale,
        },
      };
    } finally {
      bitmap.close();
    }
  } finally {
    // Navigation may destroy the document. Cleanup failure must not hide the capture failure.
    await send("restore-overlay").catch(() => undefined);
  }
}
