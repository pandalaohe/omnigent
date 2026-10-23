import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { selection } from "./fixtures";

const event = () => ({ addListener: vi.fn(), removeListener: vi.fn() });
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("trusted extension activation", () => {
  const order: string[] = [];
  const clicked = event();
  const messages = event();
  const connected = event();
  const activated = event();
  const updated = event();
  const focus = event();
  const open = vi.fn();
  const execute = vi.fn();
  const send = vi.fn();
  const access = vi.fn();
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    order.length = 0;
    open.mockImplementation(() => {
      order.push("open");
      return Promise.resolve();
    });
    access.mockImplementation(async () => {
      order.push("trust-storage");
    });
    execute.mockImplementation(async () => {
      order.push("inject");
      return [{ frameId: 0, documentId: "doc-1" }];
    });
    send.mockResolvedValue({ ok: true });
    vi.stubGlobal("chrome", {
      runtime: {
        id: "extension",
        getURL: (path: string) => `chrome-extension://extension/${path}`,
        onConnect: connected,
        onMessage: messages,
      },
      action: { onClicked: clicked },
      sidePanel: { open },
      storage: { local: { setAccessLevel: access } },
      scripting: { executeScript: execute },
      tabs: {
        onActivated: activated,
        onUpdated: updated,
        onRemoved: event(),
        sendMessage: send,
        query: vi.fn().mockResolvedValue([{ id: 7 }]),
        captureVisibleTab: vi.fn(),
      },
      windows: {
        onFocusChanged: focus,
        update: vi.fn(),
        get: vi.fn().mockResolvedValue({ focused: true }),
      },
    });
  });
  it("opens synchronously in the gesture and restricts storage before isolated top-frame injection", async () => {
    const { invoke } = await import("../src/background");
    invoke({
      id: 7,
      windowId: 3,
      url: "https://example.test/page",
      active: true,
    });
    expect(order).toEqual(["open"]);
    await vi.waitFor(() => expect(order).toEqual(["open", "trust-storage", "inject"]));
    expect(access).toHaveBeenCalledWith({ accessLevel: "TRUSTED_CONTEXTS" });
    expect(execute).toHaveBeenCalledWith({
      target: { tabId: 7, frameIds: [0] },
      world: "ISOLATED",
      files: ["picker.js"],
    });
    expect(send.mock.calls[0]?.[2]).toEqual({ documentId: "doc-1" });
  });
  it("does not inject after storage restriction fails or on restricted pages", async () => {
    const { invoke } = await import("../src/background");
    access.mockRejectedValue(new Error("No storage"));
    invoke({
      id: 7,
      windowId: 3,
      url: "https://example.test/page",
      active: true,
    });
    await vi.waitFor(() => expect(access).toHaveBeenCalled());
    expect(execute).not.toHaveBeenCalled();
    invoke({ id: 8, windowId: 3, url: "chrome://settings", active: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(execute).not.toHaveBeenCalled();
  });
  it("exposes preview only to its exact extension sidepanel, with no activation via a panel button", async () => {
    await import("../src/background");
    const connect = connected.addListener.mock.calls[0]![0];
    for (const sender of [
      { id: "extension", url: "https://example.test/" },
      {
        id: "extension",
        url: "chrome-extension://extension/sidepanel.html",
        tab: { id: 7 },
      },
      { id: "other", url: "chrome-extension://extension/sidepanel.html" },
    ]) {
      const port = {
        name: "annotation-preview",
        sender,
        disconnect: vi.fn(),
        postMessage: vi.fn(),
        onDisconnect: event(),
      };
      connect(port);
      expect(port.disconnect).toHaveBeenCalledOnce();
      expect(port.postMessage).not.toHaveBeenCalled();
    }
    const port = {
      name: "annotation-preview",
      sender: {
        id: "extension",
        url: "chrome-extension://extension/sidepanel.html",
      },
      disconnect: vi.fn(),
      postMessage: vi.fn(),
      onDisconnect: event(),
    };
    connect(port);
    expect(port.postMessage).toHaveBeenCalledOnce();
    expect(open).not.toHaveBeenCalled();
  });
  it("rejects forged iframe/document/generation messages and invalidates navigation", async () => {
    const { invoke } = await import("../src/background");
    invoke({
      id: 7,
      windowId: 3,
      url: "https://example.test/page",
      active: true,
    });
    await vi.waitFor(() => expect(send).toHaveBeenCalled());
    const generation = send.mock.calls[0]![1].generation;
    const receive = messages.addListener.mock.calls[0]![0];
    const sender = {
      id: "extension",
      tab: { id: 7, windowId: 3 },
      frameId: 0,
      documentId: "doc-1",
      url: "https://example.test/page",
    };
    for (const bad of [
      { ...sender, frameId: 2 },
      { ...sender, documentId: "doc-2" },
      { ...sender, id: "page" },
    ]) {
      const respond = vi.fn();
      expect(receive({ type: "pick", generation, selection }, bad, respond)).toBeUndefined();
      expect(respond).not.toHaveBeenCalled();
    }
    const respond = vi.fn();
    expect(
      receive({ type: "pick", generation: "stale", selection }, sender, respond),
    ).toBeUndefined();
    updated.addListener.mock.calls[0]![0](7, { status: "loading" });
    expect(receive({ type: "pick", generation, selection }, sender, respond)).toBeUndefined();
  });
  it("drops a disconnected preview port without aborting worker activation", async () => {
    const { invoke } = await import("../src/background");
    const connect = connected.addListener.mock.calls[0]![0];
    const port = {
      name: "annotation-preview",
      sender: { id: "extension", url: "chrome-extension://extension/sidepanel.html" },
      disconnect: vi.fn(),
      postMessage: vi.fn().mockImplementation(() => {
        throw new Error("Disconnected port");
      }),
      onDisconnect: event(),
    };
    expect(() => connect(port)).not.toThrow();
    invoke({ id: 7, windowId: 3, url: "https://example.test/page", active: true });
    await vi.waitFor(() => expect(send).toHaveBeenCalled());
    expect(port.postMessage).toHaveBeenCalledOnce();
  });
});
