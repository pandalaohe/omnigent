import { IDBFactory } from "fake-indexeddb";
import { afterEach, describe, expect, it, vi } from "vitest";
import { browser, key } from "./owners.fixture";
import { selection } from "./fixtures";
import type { PanelState } from "../src/protocol";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function worker() {
  vi.resetModules();
  const fake = browser();
  vi.stubGlobal("indexedDB", new IDBFactory());
  const module = await import("../src/background");
  const frames: PanelState[] = [];
  const panelSender = { id: "extension", url: "chrome-extension://extension/sidepanel.html" };
  const port = {
    name: "annotation-preview",
    sender: panelSender,
    postMessage: vi.fn((value) => {
      frames.push(JSON.parse(JSON.stringify(value)));
    }),
    disconnect: vi.fn(),
    onDisconnect: { addListener: vi.fn() },
  };
  fake.chrome.runtime.onConnect.addListener.mock.calls[0]![0](port);
  await vi.waitFor(() => expect(frames.at(-1)?.app?.auth.status).toBe("connected"));
  const receive = fake.chrome.runtime.onMessage.addListener.mock.calls[0]![0];
  const command = (command: Record<string, unknown>) =>
    new Promise<{ ok: boolean; error?: string }>((resolve) => {
      expect(receive({ type: "panel-command", command }, panelSender, resolve)).toBe(true);
    });
  const activate = async () => {
    module.invoke({
      id: 7,
      windowId: 3,
      url: "https://example.test/page?filter=active#detail",
      active: true,
    });
    await vi.waitFor(() => expect(fake.chrome.tabs.sendMessage).toHaveBeenCalled());
    return fake.chrome.tabs.sendMessage.mock.calls[0]![1].generation;
  };
  const pick = async (generation: string) =>
    new Promise((resolve) => {
      expect(
        receive(
          { type: "pick", generation, selection },
          {
            id: "extension",
            tab: { id: 7, windowId: 3 },
            frameId: 0,
            documentId: "doc-a",
            url: "https://example.test/page?filter=active#detail",
          },
          resolve,
        ),
      ).toBe(true);
    });
  return { ...fake, frames, receive, command, activate, pick, panelSender };
}

describe("actual worker message entry points", () => {
  it("accepts commands only from the exact extension panel and keeps public messages JSON-safe", async () => {
    const f = await worker();
    for (const sender of [
      { ...f.panelSender, tab: { id: 7 } },
      { id: "page", url: f.panelSender.url },
      { id: "extension", url: "https://example.test/" },
      { ...f.panelSender, url: `${f.panelSender.url}?spoof=1` },
    ]) {
      const respond = vi.fn();
      expect(
        f.receive(
          {
            type: "panel-command",
            command: { type: "connect", server: "https://evil.test", label: "page" },
          },
          sender,
          respond,
        ),
      ).toBeUndefined();
      expect(respond).not.toHaveBeenCalled();
    }
    expect(f.requests).toHaveLength(0);
    const part = f.frames.at(-1)!.app!.current!;
    expect(
      await f.command({
        type: "fullUrl",
        key,
        revision: part.revision,
        expectedTarget: null,
        enabled: true,
      }),
    ).toEqual({ ok: true });
    expect(f.frames.at(-1)!.app!.current!.includeFullURL).toBe(true);
    const serialized = JSON.stringify(f.frames);
    expect(serialized).not.toContain("synthetic-access");
    expect(serialized).not.toContain("synthetic-refresh");
    expect(serialized).not.toContain("synthetic-device");
  });
  it("derives an opted-in full URL from the Chrome sender, while rejecting page-supplied consent", async () => {
    const f = await worker();
    const part = f.frames.at(-1)!.app!.current!;
    await f.command({
      type: "fullUrl",
      key,
      revision: part.revision,
      expectedTarget: null,
      enabled: true,
    });
    const generation = await f.activate();
    const respond = vi.fn();
    expect(
      f.receive(
        {
          type: "pick",
          generation,
          selection: { ...selection, page: { ...selection.page, include_query_fragment: true } },
        },
        {
          id: "extension",
          tab: { id: 7, windowId: 3 },
          frameId: 0,
          documentId: "doc-a",
          url: "https://example.test/page?filter=active#detail",
        },
        respond,
      ),
    ).toBeUndefined();
    expect(respond).toHaveBeenCalledWith({ ok: false });
    await f.pick(generation);
    const saved = f.frames.at(-1)!.app!.current!.items[0]!;
    expect(saved.wire.page).toEqual({
      url: "https://example.test/page?filter=active#detail",
      title: "Demo",
      include_query_fragment: true,
    });
    expect(saved.wire.geometry.bitmap_scale).toBeNull();
    expect(saved.choice).toBe("pending");
    expect(f.frames.at(-1)!.preview).toBeUndefined();
    expect(f.requests).toHaveLength(0);
  });
  it("retains the temporary preview on failed IDB commit and saves it once after explicit recovery", async () => {
    const f = await worker();
    const generation = await f.activate();
    const original = IDBObjectStore.prototype.put;
    const fault = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementationOnce(function (
      this: IDBObjectStore,
      ...args: Parameters<IDBObjectStore["put"]>
    ) {
      const request = original.apply(this, args);
      this.transaction.abort();
      return request;
    });
    await f.pick(generation);
    expect(f.frames.at(-1)!.preview).toBeDefined();
    expect(f.frames.at(-1)!.message).toContain("not saved");
    expect(f.frames.at(-1)!.app!.current!.items).toEqual([]);
    fault.mockRestore();
    expect(await f.command({ type: "savePreview", key })).toEqual({ ok: true });
    expect(f.frames.at(-1)!.preview).toBeUndefined();
    expect(f.frames.at(-1)!.app!.current!.items).toHaveLength(1);
    expect(f.frames.at(-1)!.app!.current!.items[0]!.wire.page.url).toBe(
      "https://example.test/page",
    );
    expect(f.frames.at(-1)!.message).toContain("Saved");
    expect((await f.command({ type: "savePreview", key })).ok).toBe(false);
    expect(f.frames.at(-1)!.app!.current!.items).toHaveLength(1);
  });
});
