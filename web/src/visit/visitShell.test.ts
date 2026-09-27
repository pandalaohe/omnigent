// Tests for the visitor shell: config block → iframe + MessageChannel
// handshake, selection/whole-page comment POSTs with mapped offsets, the
// comments-off and handshake-timeout states, each server refusal, and draft
// persistence across a reload.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BRIDGE_MSG, BRIDGE_SOURCE, findAnchorInSource } from "@/shell/htmlCommentBridge";
import { readVisitConfig, type VisitConfig } from "./visitConfig";
import { mountVisitShell, type VisitShellWindow } from "./visitShell";

const FRAME_URL = "/v1/artifacts/h-token/reports/index.html";
const LINKED_URL = "/v1/artifacts/h-token/reports/page2.html";
const ENDPOINT = "/v1/artifact-comments";
const NONCE = "bridge-nonce";
const DRAFT_KEY = "omni-visit-draft:g-token:reports/index.html";

const PAGE_SOURCE = "<html><body><p>Hello world</p></body></html>";
const PAGE_BODY = `<html><body><p>Hello world</p><script data-omni-nonce="${NONCE}">bridge()</script></body></html>`;

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
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("visitor shell handshake", () => {
  it("posts the bridge init with the nonce and visit mode, then reveals the affordance on ready", async () => {
    mount();
    const { postMessage } = loadFrame();

    expect(postMessage).toHaveBeenCalledTimes(1);
    const [message, target, ports] = postMessage.mock.calls[0] as [
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
    expect(get("omni-visit-comment").hidden).toBe(false);
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
  it("maps a selection to source offsets and posts the anchor with the comment", async () => {
    const fetchMock = mockFetch((url) => {
      if (url === FRAME_URL) return new Response(PAGE_BODY, { status: 200 });
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
      rect: { left: 10, top: 20, right: 80, bottom: 30 },
    });

    const selectionButton = get<HTMLButtonElement>("omni-visit-selection-comment");
    expect(selectionButton.hidden).toBe(false);
    selectionButton.click();

    expect(get<HTMLDivElement>("omni-visit-anchor").textContent).toContain("Hello world");
    fillComposer("Ada", "Nice page");
    sendComment();

    await vi.waitFor(() => expect(get("omni-visit-status").textContent).toBe("Received"));
    const expected = findAnchorInSource(PAGE_SOURCE, "Hello world", 0);
    expect(expected).not.toBeNull();
    expect(postedBody(fetchMock)).toEqual({
      token: "g-token",
      grant: null,
      path: "reports/index.html",
      body: "Nice page",
      anchor_content: "Hello world",
      start_index: expected?.start_index,
      end_index: expected?.end_index,
      name: "Ada",
    });
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
      url: FRAME_URL,
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
