// Tests for the visitor shell: config block → iframe + MessageChannel
// handshake, selection/whole-page comment POSTs with mapped offsets, the
// comments-off and handshake-timeout states, each server refusal, and draft
// persistence across a reload.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BRIDGE_MSG, BRIDGE_SOURCE } from "@/shell/htmlCommentBridge";
import { ANNOTATE_MSG, ANNOTATE_SOURCE } from "@/shell/htmlAnnotateBridge";
import {
  clampElementAnchor,
  decodeElementAnchor,
  encodeElementAnchor,
} from "@/shell/annotationAnchor";
import { encodeRenderedTextAnchor } from "@/shell/renderedTextAnchor";
import { readVisitConfig, type VisitConfig } from "./visitConfig";
import { mountVisitShell, type VisitShellWindow } from "./visitShell";

vi.mock("@/shell/annotate/runtimeSources", () => ({
  loadRuntimeSources: async (part: string) => [`${part}-source`],
}));

const FRAME_URL = "/v1/artifacts/h-token/reports/index.html";
const LINKED_URL = "/v1/artifacts/h-token/reports/page2.html";
const ENDPOINT = "/v1/artifact-comments";
const NONCE = "bridge-nonce";
const DRAFT_KEY = "omni-visit-draft:g-token:reports/index.html";

function baseConfig(overrides: Partial<VisitConfig> = {}): VisitConfig {
  return {
    frameUrl: FRAME_URL,
    nonce: NONCE,
    token: "g-token",
    grant: null,
    path: "reports/index.html",
    commentsEnabled: true,
    ...overrides,
  };
}

let reload: ReturnType<typeof vi.fn<() => void>>;

function createWin(): VisitShellWindow {
  reload = vi.fn<() => void>();
  return {
    location: { reload: () => reload() },
    sessionStorage: window.sessionStorage,
    setTimeout: window.setTimeout.bind(window),
    clearTimeout: window.clearTimeout.bind(window),
  };
}

function mount(overrides: Partial<VisitConfig> = {}) {
  const config = baseConfig(overrides);
  const block = document.createElement("script");
  block.type = "application/json";
  block.id = "omni-visit-config";
  block.textContent = JSON.stringify(config);
  document.body.appendChild(block);
  const parsed = readVisitConfig(document);
  if (parsed === null) throw new Error("test config did not parse");
  return mountVisitShell(document.body, parsed, createWin());
}

function get<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`missing #${id}`);
  return node as T;
}

/** Stub the frame's contentWindow and fire the load that starts the handshake. */
function loadFrame() {
  const frame = get<HTMLIFrameElement>("omni-visit-frame");
  const postMessage = vi.fn();
  Object.defineProperty(frame, "contentWindow", {
    configurable: true,
    value: { postMessage },
  });
  frame.dispatchEvent(new Event("load"));
  return { frame, postMessage };
}

/** Send one bridge message over the port transferred at the last init. */
async function sendFromFrame(
  postMessage: ReturnType<typeof vi.fn>,
  message: Record<string, unknown>,
) {
  const call = postMessage.mock.calls.at(-1);
  if (!call) throw new Error("no init was posted");
  const init = call[0] as { nonce: string };
  const port = call[2][0] as MessagePort;
  port.postMessage({ source: BRIDGE_SOURCE, nonce: init.nonce, ...message });
  await new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

async function sendAnnotateFromFrame(
  postMessage: ReturnType<typeof vi.fn>,
  message: Record<string, unknown>,
) {
  const call = postMessage.mock.calls.find(([payload]) => payload.source === ANNOTATE_SOURCE);
  if (!call) throw new Error("no annotate init was posted");
  const port = call[2][0] as MessagePort;
  port.postMessage({ source: ANNOTATE_SOURCE, nonce: NONCE, ...message });
  await new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

function mockFetch(handlers: (url: string) => Response | Promise<Response> | null) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const response = handlers(String(input));
    if (response === null) return new Response("missing", { status: 404 });
    return response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function fillComposer(name: string, text: string) {
  const nameInput = get<HTMLInputElement>("omni-visit-name");
  const bodyInput = get<HTMLTextAreaElement>("omni-visit-body");
  nameInput.value = name;
  bodyInput.value = text;
  nameInput.dispatchEvent(new Event("input"));
  bodyInput.dispatchEvent(new Event("input"));
}

function sendComment() {
  get<HTMLButtonElement>("omni-visit-submit").click();
}

function postedBody(fetchMock: ReturnType<typeof mockFetch>) {
  const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith(ENDPOINT));
  const init = (call as unknown[] | undefined)?.[1] as RequestInit | undefined;
  return JSON.parse(String(init?.body)) as Record<string, unknown>;
}

beforeEach(() => {
  document.body.innerHTML = "";
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("visitor shell handshake", () => {
  it("posts the bridge init with the nonce and visit mode, then reveals the affordance on ready", async () => {
    mount();
    const { postMessage } = loadFrame();

    expect(postMessage).toHaveBeenCalledTimes(2);
    expect(postMessage.mock.calls[0]?.[0]).toMatchObject({
      source: "omni-html-annotate",
      type: "annotate:init",
    });
    const [message, target, ports] = postMessage.mock.calls[1] as [
      Record<string, unknown>,
      string,
      MessagePort[],
    ];
    expect(message).toMatchObject({
      source: BRIDGE_SOURCE,
      nonce: NONCE,
      type: BRIDGE_MSG.init,
      visit: true,
    });
    expect(target).toBe("*");
    expect(ports).toHaveLength(1);

    // No affordance until the bridge answers.
    expect(get("omni-visit-comment").hidden).toBe(true);

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });
    await vi.waitFor(() => expect(get("omni-visit-comment").hidden).toBe(false));
    expect(get("omni-visit-unavailable").hidden).toBe(true);
  });

  it("shows the unavailable notice when no ready arrives, and clears it on a late ready", async () => {
    vi.useFakeTimers();
    mount();
    const { postMessage } = loadFrame();

    vi.advanceTimersByTime(4000);
    expect(get("omni-visit-unavailable").hidden).toBe(false);
    expect(get("omni-visit-comment").hidden).toBe(true);

    vi.useRealTimers();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });
    expect(get("omni-visit-unavailable").hidden).toBe(true);
    expect(get("omni-visit-comment").hidden).toBe(false);
  });

  it("renders no affordance when comments are switched off", async () => {
    mount({ commentsEnabled: false });
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });

    expect(document.getElementById("omni-visit-comment")).toBeNull();
    expect(document.getElementById("omni-visit-selection-comment")).toBeNull();
  });
});

describe("visitor element annotations", () => {
  const pick = {
    type: ANNOTATE_MSG.picked,
    anchor: {
      v: 1,
      kind: "element",
      page: { url: "https://example.test/report", title: "Report" },
      target: { label: "Save button" },
      rect: { x: 1, y: 2, w: 40, h: 20 },
      screenshot: { file_id: "forged-file", filename: "old.png", width: 1, height: 1 },
    },
    viewportRect: { x: 1, y: 2, w: 40, h: 20 },
    screenshot: { dataUrl: "data:image/png;base64,AQID", width: 1, height: 1 },
  };

  it("keeps the note and element draft when the screenshot exceeds storage quota", () => {
    const elementAnchor = clampElementAnchor({ ...pick.anchor, screenshot: null });
    if (elementAnchor === null) throw new Error("invalid draft fixture");
    const anchor = encodeElementAnchor(elementAnchor);
    sessionStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        path: baseConfig().path,
        text: "Before edit",
        name: "Ada",
        anchor_content: anchor,
        screenshot: pick.screenshot,
      }),
    );
    mount();
    const setItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (JSON.parse(value).screenshot) throw new DOMException("Full", "QuotaExceededError");
      setItem.call(this, key, value);
    });
    fillComposer("Ada", "Keep this note");
    const stored = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? "null");
    expect(stored.text).toBe("Keep this note");
    expect(stored.name).toBe("Ada");
    expect(decodeElementAnchor(stored.anchor_content)).toMatchObject({
      target: { label: "Save button" },
      rect: { x: 1, y: 2, w: 40, h: 20 },
    });
    expect(stored.screenshot).toBeNull();
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
  });

  it("requires parent activation, then keeps the annotated draft on quota refusal", async () => {
    const fetchMock = mockFetch((url) =>
      url === ENDPOINT
        ? new Response(JSON.stringify({ reason: "screenshot_quota" }), { status: 429 })
        : null,
    );
    mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });
    await sendAnnotateFromFrame(postMessage, { type: ANNOTATE_MSG.ready, pathname: FRAME_URL });

    await sendAnnotateFromFrame(postMessage, pick);
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(true);

    get<HTMLButtonElement>("omni-visit-annotate").click();
    await sendAnnotateFromFrame(postMessage, {
      type: ANNOTATE_MSG.runtimeLoaded,
      part: "core",
      ok: true,
    });
    await sendAnnotateFromFrame(postMessage, {
      type: ANNOTATE_MSG.runtimeLoaded,
      part: "pick",
      ok: true,
    });
    await sendAnnotateFromFrame(postMessage, pick);
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
    expect(get("omni-visit-expiry").textContent).toContain("30 days");

    fillComposer("Ada", "Please move this button");
    sendComment();
    await vi.waitFor(() =>
      expect(get("omni-visit-error").textContent).toContain("100 screenshot limit"),
    );
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
    expect(get<HTMLTextAreaElement>("omni-visit-body").value).toBe("Please move this button");
    expect(postedBody(fetchMock).screenshot).toEqual(
      pick.screenshot && {
        data_url: pick.screenshot.dataUrl,
        width: 1,
        height: 1,
      },
    );
    expect(String(postedBody(fetchMock).anchor_content)).toContain("__element__");
    expect(String(postedBody(fetchMock).anchor_content)).not.toContain("forged-file");
  });
});

describe("visitor shell visibility", () => {
  it("keeps the form actually hidden until it is opened", async () => {
    mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });

    const form = get<HTMLFormElement>("omni-visit-form");
    expect(form.hidden).toBe(true);
    // The shell's own `display: flex` must not override the hidden attribute.
    expect(getComputedStyle(form).display).toBe("none");

    get<HTMLButtonElement>("omni-visit-comment").click();
    expect(form.hidden).toBe(false);
    expect(getComputedStyle(form).display).not.toBe("none");
  });
});

describe("visitor shell comments", () => {
  it("restores a pre-upgrade selection draft without losing its body or inventing a location", async () => {
    window.sessionStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        path: "reports/index.html",
        url: FRAME_URL,
        text: "Keep this thought",
        name: "Ada",
        anchor_content: "Code",
        occ: 1,
      }),
    );
    const fetchMock = mockFetch((url) =>
      url === ENDPOINT ? new Response(JSON.stringify({ ok: true }), { status: 201 }) : null,
    );
    mount();
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
    expect(get<HTMLTextAreaElement>("omni-visit-body").value).toBe("Keep this thought");
    expect(get<HTMLInputElement>("omni-visit-name").value).toBe("Ada");
    expect(get<HTMLDivElement>("omni-visit-anchor").textContent).toContain(
      "This comment will be about the whole page.",
    );
    get<HTMLTextAreaElement>("omni-visit-body").dispatchEvent(
      new Event("input", { bubbles: true }),
    );
    expect(JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) ?? "null")).toMatchObject({
      text: "Keep this thought",
      name: "Ada",
      anchor_content: null,
      legacyQuote: "Code",
    });
    sendComment();
    await vi.waitFor(() => expect(get("omni-visit-status").textContent).toBe("Received"));
    expect(postedBody(fetchMock)).toMatchObject({
      body: "Keep this thought",
      name: "Ada",
      start_index: 0,
      end_index: 0,
    });
    expect(postedBody(fetchMock)).not.toHaveProperty("anchor_content");
  });

  it("posts a rendered selection without guessing source offsets", async () => {
    const fetchMock = mockFetch((url) => {
      if (url === ENDPOINT) return new Response(JSON.stringify({ ok: true }), { status: 201 });
      return null;
    });
    mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "Hello world",
      occ: 0,
      prefix: "Before ",
      suffix: " After",
      selector: "#visit-text",
      rect: { left: 10, top: 20, right: 80, bottom: 30 },
    });

    const selectionButton = get<HTMLButtonElement>("omni-visit-selection-comment");
    expect(selectionButton.hidden).toBe(false);
    selectionButton.click();

    expect(get<HTMLDivElement>("omni-visit-anchor").textContent).toContain("Hello world");
    fillComposer("Ada", "Nice page");
    sendComment();

    await vi.waitFor(() => expect(get("omni-visit-status").textContent).toBe("Received"));
    expect(postedBody(fetchMock)).toEqual({
      token: "g-token",
      grant: null,
      path: "reports/index.html",
      body: "Nice page",
      anchor_content: encodeRenderedTextAnchor({
        v: 1,
        kind: "html",
        text: "Hello world",
        occ: 0,
        prefix: "Before ",
        suffix: " After",
        selector: "#visit-text",
      }),
      start_index: 0,
      end_index: 0,
      name: "Ada",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // A 201 clears the draft and the composer.
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(true);
    expect(window.sessionStorage.getItem(DRAFT_KEY)).toBeNull();
  });

  it("posts a whole-page comment against the page the frame navigated to, with no anchor", async () => {
    const fetchMock = mockFetch((url) => {
      if (url.endsWith(ENDPOINT)) {
        return new Response(JSON.stringify({ ok: true }), { status: 201 });
      }
      return null;
    });
    mount({ frameUrl: `/proxy${FRAME_URL}`, grant: "grant-1" });
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.ready,
      pathname: `/proxy${LINKED_URL}`,
    });

    get<HTMLButtonElement>("omni-visit-comment").click();
    expect(get<HTMLDivElement>("omni-visit-anchor").hidden).toBe(true);
    fillComposer("", "Whole page");
    sendComment();

    await vi.waitFor(() => expect(get("omni-visit-status").textContent).toBe("Received"));
    const body = postedBody(fetchMock);
    expect(body).toEqual({
      token: "g-token",
      grant: "grant-1",
      path: "reports/page2.html",
      body: "Whole page",
      start_index: 0,
      end_index: 0,
      name: null,
    });
    expect("anchor_content" in body).toBe(false);
    // Nothing to map without an anchor: no source fetch, no endpoint prefix lost.
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([`/proxy${ENDPOINT}`]);
  });

  it.each([
    [403, JSON.stringify({ reason: "disabled" }), "Comments are turned off for this page"],
    [410, "gone", "This link is no longer available"],
    [429, "slow down", "Too many comments from this link, try again later"],
    [500, "boom", "Couldn't send your comment. Try again."],
  ])("shows the refusal for %s and keeps the text", async (status, payload, message) => {
    mockFetch((url) =>
      url === ENDPOINT ? new Response(payload, { status: status as number }) : null,
    );
    mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });

    get<HTMLButtonElement>("omni-visit-comment").click();
    fillComposer("", "Still mine");
    sendComment();

    await vi.waitFor(() => expect(get("omni-visit-error").textContent).toBe(message));
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
    expect(get<HTMLTextAreaElement>("omni-visit-body").value).toBe("Still mine");
    // Offer retry: the send button is usable again.
    expect(get<HTMLButtonElement>("omni-visit-submit").disabled).toBe(false);
  });

  it("offers a retry when the POST never reaches the server", async () => {
    mockFetch((url) => {
      if (url === ENDPOINT) return Promise.reject(new TypeError("Failed to fetch"));
      return null;
    });
    mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });

    get<HTMLButtonElement>("omni-visit-comment").click();
    fillComposer("", "Please retry");
    sendComment();

    await vi.waitFor(() =>
      expect(get("omni-visit-error").textContent).toBe(
        "Couldn't send your comment. Check your connection and try again.",
      ),
    );
    expect(get<HTMLTextAreaElement>("omni-visit-body").value).toBe("Please retry");
    expect(get<HTMLButtonElement>("omni-visit-submit").disabled).toBe(false);
  });

  it("saves the draft and reloads the top-level page on a stale grant", async () => {
    mockFetch((url) =>
      url === ENDPOINT ? new Response(JSON.stringify({ reason: "reload" }), { status: 403 }) : null,
    );
    const shell = mount();
    const { postMessage } = loadFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: FRAME_URL });

    get<HTMLButtonElement>("omni-visit-comment").click();
    fillComposer("Ada", "Keep me");
    sendComment();

    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) ?? "null")).toMatchObject({
      path: "reports/index.html",
      text: "Keep me",
      name: "Ada",
    });

    // The reload tears the shell down; the fresh mount restores the draft.
    shell.destroy();
    document.body.innerHTML = "";
    mockFetch(() => null);
    mount();
    expect(get<HTMLFormElement>("omni-visit-form").hidden).toBe(false);
    expect(get<HTMLTextAreaElement>("omni-visit-body").value).toBe("Keep me");
    expect(get<HTMLInputElement>("omni-visit-name").value).toBe("Ada");
  });
});
