// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const add = vi.fn();
const remove = vi.fn();
const send = vi.fn();
const sender = { id: "extension" };
beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  send.mockReset().mockResolvedValue({ ok: true });
  vi.stubGlobal("chrome", {
    runtime: {
      id: "extension",
      getURL: (path: string) => `chrome-extension://extension/${path}`,
      onMessage: { addListener: add, removeListener: remove },
      sendMessage: send,
    },
  });
  vi.stubGlobal("visualViewport", undefined);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  // jsdom has no popover rendering; only the lifecycle call is represented here.
  Object.defineProperty(HTMLElement.prototype, "showPopover", {
    configurable: true,
    value: vi.fn(),
  });
  const matches = Element.prototype.matches;
  vi.spyOn(Element.prototype, "matches").mockImplementation(function (
    this: Element,
    selector: string,
  ) {
    return selector === ":popover-open" ? false : matches.call(this, selector);
  });
  const query = document.querySelector.bind(document);
  vi.spyOn(document, "querySelector").mockImplementation((selector: string) =>
    selector === "dialog[open]:modal" ? query("dialog[open]") : query(selector),
  );
});
afterEach(() => {
  const listener = add.mock.calls.at(-1)?.[0];
  listener?.({ type: "dispose" }, sender, () => undefined);
  document.body.replaceChildren();
  vi.restoreAllMocks();
  Reflect.deleteProperty(HTMLElement.prototype, "showPopover");
  vi.unstubAllGlobals();
});

describe("picker trusted-event and lifecycle boundary", () => {
  it("constructs via DOM APIs under a rejected HTML sink, ignores synthetic capture events, and removes its listener", async () => {
    vi.spyOn(Element.prototype, "innerHTML", "set").mockImplementation(() => {
      throw new Error("Trusted Types HTML sink rejected");
    });
    await import("../src/picker/index");
    expect(document.querySelector("[popover]")).not.toBeNull();
    const listener = add.mock.calls[0]![0];
    const respond = vi.fn();
    listener({ type: "activate", generation: "g" }, sender, respond);
    await vi.waitFor(() => expect(respond).toHaveBeenCalledWith({ ok: true }));
    const target = document.createElement("button");
    target.textContent = "Page button";
    document.body.append(target);
    for (const type of ["pointerdown", "pointerup", "click"])
      target.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: 10, clientY: 10 }));
    expect(send).not.toHaveBeenCalled();
    listener({ type: "dispose", generation: "g" }, sender, respond);
    expect(remove).toHaveBeenCalledWith(listener);
    expect(document.querySelector("[popover]")).toBeNull();
  });
  it("rejects page/content/other-extension activation and permits teardown before activation completes", async () => {
    await import("../src/picker/index");
    const listener = add.mock.calls[0]![0];
    const respond = vi.fn();
    for (const bad of [
      { id: "other" },
      { id: "extension", tab: { id: 7 } },
      { id: "extension", url: "https://example.test/" },
    ]) {
      listener({ type: "activate", generation: "g" }, bad, respond);
    }
    expect(respond).not.toHaveBeenCalled();
    listener({ type: "dispose", generation: "unused" }, sender, respond);
    expect(document.querySelector("[popover]")).toBeNull();
  });
  it("cleans up an earlier injected picker and places the toolbar inside an active dialog", async () => {
    const dialog = document.createElement("dialog");
    dialog.open = true;
    document.body.append(dialog);
    await import("../src/picker/index");
    const previous = add.mock.calls[0]![0];
    vi.resetModules();
    await import("../src/picker/index");
    expect(remove).toHaveBeenCalledWith(previous);
    expect(document.querySelectorAll("[popover]")).toHaveLength(1);
    const respond = vi.fn();
    add.mock.calls.at(-1)![0]({ type: "activate", generation: "g" }, sender, respond);
    await vi.waitFor(() => expect(respond).toHaveBeenCalledWith({ ok: true }));
    expect(dialog.querySelector("[popover]")).not.toBeNull();
  });
});

// Invoke the module's registered callbacks with boundary records; jsdom cannot
// produce trusted physical pointer events or implement hit testing/capture.
async function regionFixture() {
  const iframe = document.createElement("iframe");
  document.body.append(iframe);
  const events = vi.spyOn(EventTarget.prototype, "addEventListener");
  const attach = vi.spyOn(Element.prototype, "attachShadow");
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1000);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(800);
  vi.stubGlobal("innerWidth", 1000);
  vi.stubGlobal("innerHeight", 800);
  await import("../src/picker/index");
  const root = attach.mock.results[0]!.value as ShadowRoot;
  const surface = root.querySelector<HTMLElement>(".region-surface")!;
  const toolbar = root.querySelector<HTMLElement>(".bar")!;
  const host = root.host as HTMLElement;
  let captured: number | undefined;
  const set = vi.fn((pointerId: number) => {
    captured = pointerId;
  });
  const release = vi.fn((pointerId: number) => {
    if (captured === pointerId) captured = undefined;
  });
  Object.defineProperties(surface, {
    setPointerCapture: { value: set },
    hasPointerCapture: { value: (id: number) => captured === id },
    releasePointerCapture: { value: release },
  });
  const registration = (node: EventTarget, type: string) => {
    const index = events.mock.calls.findIndex(
      (call, index) => call[0] === type && events.mock.contexts[index] === node,
    );
    if (index < 0) throw new Error(`No ${type} listener`);
    return events.mock.calls[index]!;
  };
  const fire = (node: EventTarget, type: string, values: Record<string, unknown> = {}) => {
    const listener = registration(node, type)[1];
    if (typeof listener !== "function") throw new Error("Expected callback");
    const event = {
      isTrusted: true,
      button: 0,
      pointerId: 9,
      clientX: 100,
      clientY: 200,
      preventDefault: vi.fn(),
      stopImmediatePropagation: vi.fn(),
      composedPath: () => [host, document.body, document, window],
      ...values,
    };
    listener.call(node, event as unknown as Event);
    return event;
  };
  const choose = (label: string) => {
    const button = Array.from(toolbar.querySelectorAll("button")).find(
      (node) => node.textContent === label,
    )!;
    fire(button, "click");
  };
  const worker = add.mock.calls[0]![0];
  const respond = vi.fn();
  worker({ type: "activate", generation: "g" }, sender, respond);
  await vi.waitFor(() => expect(respond).toHaveBeenCalledWith({ ok: true }));
  return {
    root,
    surface,
    toolbar,
    host,
    set,
    release,
    fire,
    choose,
    worker,
    registration,
    loseCapture: () => {
      captured = undefined;
    },
  };
}

describe("region input surface over embedded content (API boundary)", () => {
  it("accepts a captured surface drag despite closed-shadow retargeting, excludes the toolbar, and hides with the overlay", async () => {
    const fixture = await regionFixture();
    const { surface, root, toolbar, host, choose, fire, set, release, worker } = fixture;
    expect(root.mode).toBe("closed");
    expect(host.shadowRoot).toBeNull();
    expect(surface.hidden).toBe(true);
    choose("Region");
    expect(surface.hidden).toBe(false);
    expect(
      ["position", "inset", "pointer-events", "touch-action"].map((name) =>
        surface.style.getPropertyValue(name),
      ),
    ).toEqual(["fixed", "0px", "auto", "none"]);
    expect(surface.parentNode).toBe(root);
    expect(toolbar.parentNode).toBe(root);
    expect(surface.contains(toolbar)).toBe(false);
    const toolbarUp = fire(document, "pointerup");
    expect(toolbarUp.preventDefault).not.toHaveBeenCalled();
    expect(set).not.toHaveBeenCalled();
    fire(surface, "pointerdown", { isTrusted: false });
    expect(set).not.toHaveBeenCalled();
    fire(surface, "pointerdown");
    expect(set).toHaveBeenCalledWith(9);
    fire(surface, "pointermove", { clientX: 180, clientY: 260 });
    expect(
      ["left", "top", "width", "height"].map((name) =>
        root.querySelector<HTMLElement>(".box")!.style.getPropertyValue(name),
      ),
    ).toEqual(["100px", "200px", "80px", "60px"]);
    fire(surface, "pointerup", { pointerId: 10, clientX: 180, clientY: 260 });
    expect(send).not.toHaveBeenCalled();
    fire(surface, "pointerup", { clientX: 180, clientY: 260 });
    expect(release).toHaveBeenCalledWith(9);
    expect(send).toHaveBeenCalledOnce();
    expect(send.mock.calls[0]![0]).toMatchObject({
      type: "pick",
      generation: "g",
      selection: { kind: "region", rect: { x: 100, y: 200, width: 80, height: 60 } },
    });
    const prepared = vi.fn();
    worker({ type: "prepare-capture", generation: "g" }, sender, prepared);
    expect(host.style.visibility).toBe("hidden");
    await vi.waitFor(() => expect(prepared).toHaveBeenCalled());
    worker({ type: "restore-overlay", generation: "g" }, sender, () => undefined);
    expect(host.style.visibility).toBe("visible");
  });
  it.each(["mode", "cancel", "lost", "blur", "dispose"])(
    "cancels the drag on %s without emitting stale evidence",
    async (reason) => {
      const { surface, choose, fire, release, worker, registration, loseCapture } =
        await regionFixture();
      choose("Region");
      fire(surface, "pointerdown");
      if (reason === "mode") {
        choose("Text");
        expect(surface.hidden).toBe(true);
        choose("Element");
        expect(surface.hidden).toBe(true);
      } else if (reason === "cancel") fire(surface, "pointercancel");
      else if (reason === "lost") {
        loseCapture();
        fire(surface, "lostpointercapture");
      } else if (reason === "blur") window.dispatchEvent(new Event("blur"));
      else worker({ type: "dispose" }, sender, () => undefined);
      expect(release).toHaveBeenCalledTimes(reason === "lost" ? 0 : 1);
      fire(surface, "pointerup", { clientX: 180, clientY: 260 });
      expect(send).not.toHaveBeenCalled();
      if (reason === "dispose")
        expect(
          (registration(surface, "pointerdown")[2] as AddEventListenerOptions).signal?.aborted,
        ).toBe(true);
    },
  );
  it("reports pointer-capture loss at drag start and does not emit a partial region", async () => {
    const { surface, set, fire, choose, root } = await regionFixture();
    choose("Region");
    set.mockImplementation(() => {
      throw new Error("Pointer no longer active");
    });
    fire(surface, "pointerdown");
    fire(surface, "pointerup", { clientX: 180, clientY: 260 });
    expect(send).not.toHaveBeenCalled();
    expect(root.querySelector(".help")!.textContent).toBe("Region drag ended. Drag again.");
  });
});
