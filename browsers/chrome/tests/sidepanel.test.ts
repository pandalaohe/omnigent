// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { identityKey, newPartition } from "../src/model";
import type { AppView, PartitionView } from "../src/model";
import { identity, item, receipt, snapshot, target, targets } from "./owners.fixture";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function panel() {
  vi.resetModules();
  document.documentElement.innerHTML = readFileSync("sidepanel.html", "utf8");
  const listeners = new Map<EventTarget, Map<string, EventListener[]>>();
  const add = EventTarget.prototype.addEventListener;
  vi.spyOn(EventTarget.prototype, "addEventListener").mockImplementation(function (
    this: EventTarget,
    type,
    listener,
    options,
  ) {
    if (typeof listener === "function") {
      const map = listeners.get(this) ?? new Map();
      map.set(type, [...(map.get(type) ?? []), listener]);
      listeners.set(this, map);
    }
    add.call(this, type, listener, options);
  });
  const messages = { addListener: vi.fn() };
  const disconnected = { addListener: vi.fn() };
  const send = vi.fn(async (_input: unknown) => ({ ok: true }));
  const permission = vi.fn(async () => true);
  vi.stubGlobal("chrome", {
    permissions: { request: permission },
    runtime: {
      sendMessage: send,
      connect: vi.fn(() => ({ onMessage: messages, onDisconnect: disconnected })),
    },
  });
  await import("../src/sidepanel/index");
  // jsdom cannot create trusted physical input. Invoke the registered API-boundary callback only.
  const input = (target: EventTarget, type: string, extra: Record<string, unknown> = {}) => {
    for (const callback of listeners.get(target)?.get(type) ?? [])
      callback.call(target, {
        isTrusted: true,
        preventDefault() {},
        target,
        currentTarget: target,
        ...extra,
      } as unknown as Event);
  };
  const push = (view: AppView) =>
    messages.addListener.mock.calls[0]![0](
      JSON.parse(JSON.stringify({ app: view, message: view.message })),
    );
  return { input, push, send, permission };
}
function view(): AppView {
  const part: PartitionView = {
    ...newPartition(identity),
    bytes: 0,
    target,
    items: [{ wire: item().wire, choice: "without", bytes: 0 }],
    batches: [],
  };
  return {
    auth: { status: "connected", identity, server: identity.server },
    current: part,
    old: [],
    targets: targets(),
    message: "Saved locally",
    busy: [],
  };
}
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const button = (text: string) =>
  Array.from(document.querySelectorAll("button")).find((node) => node.textContent === text)!;

describe("extension panel DOM and API boundaries", () => {
  it("labels nullable host/workspace metadata while keeping an unavailable receiver pinned", async () => {
    const f = await panel();
    const next = view();
    const pinned = {
      ...target,
      id: "receiver",
      host_name: null,
      workspace: null,
      available: false,
      reason: "offline",
    };
    const noHost = {
      ...target,
      id: "hostless",
      host_name: null,
      available: false,
      reason: "host_missing",
    };
    const noWorkspace = { ...target, id: "no-workspace", workspace: null };
    next.current!.target = pinned;
    next.targets = targets(pinned, [noHost, noWorkspace, target]);
    f.push(next);
    const choices = Array.from(byId("targets").querySelectorAll("button"));
    expect(choices).toHaveLength(4);
    expect(choices[0]!.textContent).toBe(
      "Receiving in Omnigent · Demo · No host · receiver\nNo workspace · Unavailable: offline",
    );
    expect(choices[0]!.getAttribute("aria-pressed")).toBe("true");
    expect(choices[1]!.textContent).toContain("No host");
    expect(choices[2]!.textContent).toContain("No workspace");
    expect(byId("targets").textContent).not.toContain("null");
    expect(byId<HTMLButtonElement>("send").disabled).toBe(true);
    f.input(choices[2]!, "click");
    expect(f.send).toHaveBeenCalledWith({
      type: "panel-command",
      command: {
        type: "target",
        key: next.current!.key,
        revision: 0,
        expectedTarget: "receiver",
        sessionId: "no-workspace",
      },
    });
  });
  it("requests only the chosen origin synchronously in the trusted form callback before a worker hop", async () => {
    const f = await panel();
    byId<HTMLInputElement>("server").value = `${identity.server}/`;
    byId<HTMLInputElement>("label").value = "Browser";
    let resolve!: (allowed: boolean) => void;
    f.permission.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    f.input(byId("connect-form"), "submit");
    expect(f.permission).toHaveBeenCalledWith({ origins: ["https://omni.example.test/*"] });
    expect(f.send).not.toHaveBeenCalled();
    resolve(true);
    await vi.waitFor(() =>
      expect(f.send).toHaveBeenCalledWith({
        type: "panel-command",
        command: { type: "connect", server: identity.server, label: "Browser" },
      }),
    );
    f.send.mockClear();
    f.input(byId("connect-form"), "submit", { isTrusted: false });
    expect(f.send).not.toHaveBeenCalled();
    f.permission.mockResolvedValueOnce(false);
    f.input(byId("connect-form"), "submit");
    await vi.waitFor(() => expect(byId("status").textContent).toContain("declined"));
    expect(f.send).not.toHaveBeenCalled();
  });
  it("renders page strings as text, retains typed input after failed save and disables Send until notes commit", async () => {
    const f = await panel();
    const next = view();
    next.current!.items[0]!.wire.page.title = '<img src=x onerror="window.injected=true">';
    f.push(next);
    expect(document.querySelector("img[src=x]")).toBeNull();
    const note = document.querySelector("textarea")!;
    note.value = "unsaved user instruction";
    f.input(note, "input");
    expect(byId<HTMLButtonElement>("send").disabled).toBe(true);
    f.send.mockResolvedValueOnce({ ok: false } as { ok: boolean });
    f.input(button("Save note"), "click");
    await vi.waitFor(() => expect(byId("status").textContent).toContain("disconnected"));
    next.current!.revision++;
    f.push(next);
    expect(document.querySelector("textarea")!.value).toBe("unsaved user instruction");
    expect(byId<HTMLButtonElement>("send").disabled).toBe(true);
    f.input(button("Save note"), "click");
    await vi.waitFor(() => expect(byId<HTMLButtonElement>("send").disabled).toBe(false));
    expect(f.send.mock.calls.at(-1)![0]).toMatchObject({
      command: {
        type: "note",
        note: "unsaved user instruction",
        key: identityKey(identity),
        expectedTarget: target.id,
      },
    });
  });
  it("exposes an explicit full URL choice and includes the displayed identity, revision and target on Send", async () => {
    const f = await panel();
    const next = view();
    f.push(next);
    byId<HTMLInputElement>("full-url").checked = true;
    f.input(byId("full-url"), "change");
    expect(f.send).toHaveBeenCalledWith({
      type: "panel-command",
      command: {
        type: "fullUrl",
        key: next.current!.key,
        revision: 0,
        expectedTarget: target.id,
        enabled: true,
      },
    });
    f.input(byId("send"), "click");
    expect(f.send).toHaveBeenCalledWith({
      type: "panel-command",
      command: { type: "send", key: next.current!.key, revision: 0, expectedTarget: target.id },
    });
    expect(byId("send").textContent).toBe("Send 1 annotations to Demo · Workstation");
  });
  it("keeps Unknown read-only by default, uses a constructed same-server session link and disables unavailable image copies", async () => {
    const f = await panel();
    const next = view();
    const batch = snapshot();
    next.current!.batches = [{ ...batch, state: "delivery_unknown", items: [] }];
    f.push(next);
    expect(button("Check delivery")).toBeDefined();
    expect(button("Retry same batch and target")).toBeUndefined();
    next.current!.revision++;
    next.current!.batches[0] = {
      ...batch,
      state: "accepted",
      receipt: receipt(undefined, "ready") as never,
      items: [],
    };
    f.push(next);
    const link = Array.from(document.querySelectorAll("a")).find(
      (node) => node.textContent === "Open the actual session",
    )!;
    expect(link.href).toBe(`${identity.server}/c/${target.id}`);
    expect(document.body.textContent).toContain("This does not mean the agent consumed it");
    expect(button("Retry first dispatch")).toBeDefined();
    expect(button("Send another copy (possible duplicate)").disabled).toBe(true);
    expect(
      Array.from(document.querySelectorAll("a")).find(
        (node) => node.textContent === "Open Connected apps / delivery check",
      )!.href,
    ).toBe(`${identity.server}/settings/account#connected-apps-heading`);
  });
});
