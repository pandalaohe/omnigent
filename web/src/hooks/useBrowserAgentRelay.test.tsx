import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// supportsBrowser gates the whole relay; force it true so the hook registers.
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
  supportsBrowser: () => true,
}));

// The relay POSTs claim + result through authenticatedFetch; mock it so we can
// script the claim response and inspect the result POST body.
const authenticatedFetch = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (...args: unknown[]) => getSessionSlim(...args),
}));
import { setSessionHost, setSessionParent } from "@/lib/sessionHost";

import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { browserViewId, useBrowserTabs } from "./useBrowserTabs";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { useBrowserAgentRelay } from "./useBrowserAgentRelay";

const CONV = "conv_relay";
const renderRelay = (visibleId: string | null | undefined = CONV, client = new QueryClient()) => {
  return renderHook(({ id }) => useBrowserAgentRelay(id), {
    initialProps: { id: visibleId },
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
};

/** Build a `browser.action_request` event for the bus. */
function actionEvent(
  action: string,
  args: Record<string, unknown> = {},
  actionId = "baction_1",
): BrowserActionRequestEvent {
  return { type: "browser_action_request", actionId, action, args };
}

/** A Response-like stub for authenticatedFetch. */
function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** Install a `window.omnigentDesktop` bridge; returns the mock so tests assert
 *  on the exact calls / scripted JS. */
function installBridge(overrides: Record<string, unknown> = {}) {
  const bridge = {
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserScreenshot: vi
      .fn()
      .mockResolvedValue({ ok: true, dataUrl: "data:image/png;base64,AAA" }),
    browserExecute: vi.fn().mockResolvedValue({ ok: true, result: "ok" }),
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    browserClose: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return bridge;
}

/** Mount the relay and dispatch one action through the bus, then wait for the
 *  full claim → dispatch → result chain to settle. When the claim is expected
 *  to win, wait for the result POST; otherwise (drop paths) just wait for the
 *  single claim fetch to have fired. */
async function runAction(
  evt: BrowserActionRequestEvent,
  opts: { expectResult?: boolean; source?: string } = {},
): Promise<void> {
  const { expectResult = true, source = CONV } = opts;
  renderRelay();
  emitBrowserActionRequest(evt, source);
  if (expectResult) {
    await vi.waitFor(() => {
      expect(
        authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
      ).toBe(true);
    });
  } else {
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
    // Give the (dropped) handler a couple of turns to prove it does nothing more.
    await Promise.resolve();
    await Promise.resolve();
  }
}

/** The claim_token the winning-claim response carries in most tests. */
const WON = jsonResponse({ claimed: true, claim_token: "tok_1" });

/** Parse the JS string passed to browserExecute for the Nth call. */
function executedJs(bridge: { browserExecute: ReturnType<typeof vi.fn> }, n = 0): string {
  return bridge.browserExecute.mock.calls[n][1] as string;
}

/** Read the result body POSTed back for the last action_result call. */
function postedResult(): Record<string, unknown> {
  const call = [...authenticatedFetch.mock.calls]
    .reverse()
    .find((c) => String(c[0]).includes("/browser/action_result/"));
  if (!call) throw new Error("no action_result POST recorded");
  return JSON.parse((call[1] as { body: string }).body) as Record<string, unknown>;
}

const resultCount = () =>
  authenticatedFetch.mock.calls.filter((c) => String(c[0]).includes("/browser/action_result/"))
    .length;

async function sendClaimed(action: string, args: Record<string, unknown>, count: number) {
  act(() => emitBrowserActionRequest(actionEvent(action, args, `action-${count}`), CONV));
  await vi.waitFor(() => expect(resultCount()).toBe(count));
  return postedResult().result as Record<string, unknown>;
}

beforeEach(() => {
  authenticatedFetch.mockReset();
  getSessionSlim.mockReset().mockImplementation(async (id: string) => ({
    id,
    hostId: null,
    parentSessionId: null,
  }));
  for (const id of [CONV, "conv_visible_B", "conv_background_A", "parent", "middle", "root"]) {
    setSessionHost(id, null);
    setSessionParent(id, null);
  }
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("useBrowserAgentRelay — claim-first protocol", () => {
  it("navigates the user-selected tab after winning the claim", async () => {
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-one"],
      selectedBrowserId: "tab-one",
    });
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    await runAction(actionEvent("navigate", { url: "https://example.com/next" }));

    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-one"),
      "https://example.com/next",
      undefined,
      { force: true, agent: true },
    );
  });

  it("drops the action when the claim is lost (no dispatch, no result POST)", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(jsonResponse({ claimed: false }));

    await runAction(actionEvent("navigate", { url: "https://example.com" }), {
      expectResult: false,
    });

    // Only the claim fetch fired; no dispatch, no result POST.
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(
      authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
    ).toBe(false);
  });

  it("drops the action when the claim call is not ok", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(jsonResponse({}, false));

    await runAction(actionEvent("screenshot"), { expectResult: false });

    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
  });

  it("drops the action when the claim fetch throws", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockRejectedValueOnce(new Error("network"));

    await runAction(actionEvent("screenshot"), { expectResult: false });

    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
  });

  it("on a won claim, dispatches and POSTs the result with the claim token", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    await runAction(actionEvent("navigate", { url: "https://example.com" }));

    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "https://example.com",
      undefined,
      {
        force: true,
        agent: true,
      },
    );
    const body = postedResult();
    expect(body.claim_token).toBe("tok_1");
    expect((body.result as { ok: boolean }).ok).toBe(true);
  });

  it("routes claim, dispatch, and result to the delivering conversation, not the mounted one", async () => {
    // A background conversation (A) issues a browser action while a different
    // conversation (B) is on screen and owns the relay. Every hop must target A:
    // claiming at B is rejected as an owner mismatch, so nothing executes and
    // A's browser tool times out. This is what background streams made reachable.
    const VISIBLE = "conv_visible_B";
    const BACKGROUND = "conv_background_A";
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    renderRelay(VISIBLE);
    emitBrowserActionRequest(actionEvent("navigate", { url: "https://a" }), BACKGROUND);

    await vi.waitFor(() => {
      expect(
        authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
      ).toBe(true);
    });

    const claimUrl = String(
      authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/browser/action_claim/"),
      )![0],
    );
    expect(claimUrl).toContain(`/v1/sessions/${BACKGROUND}/browser/action_claim/`);
    expect(claimUrl).not.toContain(VISIBLE);

    // Dispatch targeted A's WebContentsView, and the result posted to A.
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(BACKGROUND, "https://a", undefined, {
      force: true,
      agent: true,
    });
    const resultUrl = String(
      authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/browser/action_result/"),
      )![0],
    );
    expect(resultUrl).toContain(`/v1/sessions/${BACKGROUND}/browser/action_result/`);
  });

  it("completes the source session's screenshot when the visible session changes mid-claim", async () => {
    const bridge = installBridge();
    let resolveClaim!: (response: Response) => void;
    authenticatedFetch
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveClaim = resolve;
          }),
      )
      .mockResolvedValue(jsonResponse({}));
    const hook = renderRelay(CONV);
    emitBrowserActionRequest(actionEvent("screenshot"), "conv_background_A");
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    hook.rerender({ id: "conv_visible_B" });
    resolveClaim(WON);
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: true,
        data_url: "data:image/png;base64,AAA",
        data: { tab_id: "agent-browser" },
      }),
    );
    expect(bridge.browserScreenshot).toHaveBeenCalledWith("conv_background_A");
    expect(authenticatedFetch.mock.calls[0][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_claim/",
    );
    expect(authenticatedFetch.mock.calls[1][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_result/",
    );
  });

  it("cancels a pending claimed action when leaving all conversations", async () => {
    const bridge = installBridge();
    let resolveClaim!: (response: Response) => void;
    authenticatedFetch
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveClaim = resolve;
          }),
      )
      .mockResolvedValue(jsonResponse({}));
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), "conv_background_A");
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    hook.rerender({ id: null });
    resolveClaim(WON);
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: false,
        error: "browser relay context changed",
      }),
    );
    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
    emitBrowserActionRequest(actionEvent("screenshot", {}, "later"), "conv_background_A");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});

describe("useBrowserAgentRelay — session tabs", () => {
  const renderWithTabs = () => {
    const client = new QueryClient();
    return renderHook(
      () => {
        const tabs = useBrowserTabs(CONV);
        useBrowserAgentRelay(CONV);
        return tabs;
      },
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );
  };

  it("follows the user's selected tab through snapshot, click, type, and screenshot", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn(async (id: string) => ({
        exists: true,
        title: id.endsWith("tab-first") ? "First page" : "Second page",
        url: id.endsWith("tab-first") ? "https://example.com/first" : "https://example.com/second",
      })),
      browserExecute: vi.fn(async (_id: string, js: string) => ({
        ok: true,
        result: js.includes("__omni_snapshot_id__ =")
          ? JSON.stringify({ snapshot_id: "snapshot-second", tree: "- button [ref=1]" })
          : "ok",
      })),
    });
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-first", "tab-second"],
      selectedBrowserId: "tab-second",
    });
    const hook = renderWithTabs();
    expect(hook.result.current.selected).toBe("tab-second");

    const snapshot = await sendClaimed("snapshot", {}, 1);
    expect(bridge.browserExecute).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-second"),
      expect.any(String),
    );
    expect(snapshot.data).toMatchObject({
      tab_id: "tab-second",
      snapshot_id: "snapshot-second",
      tabs: [
        {
          tab_id: "tab-first",
          title: "First page",
          url: "https://example.com/first",
          selected: false,
        },
        {
          tab_id: "tab-second",
          title: "Second page",
          url: "https://example.com/second",
          selected: true,
        },
      ],
    });
    expect(bridge.browserHasView.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      browserViewId(CONV, "tab-first"),
      browserViewId(CONV, "tab-second"),
    ]);

    expect(await sendClaimed("click", { selector: "button.save" }, 2)).toMatchObject({ ok: true });
    expect(bridge.browserExecute).toHaveBeenLastCalledWith(
      browserViewId(CONV, "tab-second"),
      expect.any(String),
    );
    expect(await sendClaimed("type", { selector: "input.name", text: "Mira" }, 3)).toMatchObject({
      ok: true,
    });
    expect(bridge.browserExecute).toHaveBeenLastCalledWith(
      browserViewId(CONV, "tab-second"),
      expect.any(String),
    );
    expect(await sendClaimed("screenshot", {}, 4)).toMatchObject({
      ok: true,
      data: { tab_id: "tab-second" },
    });
    expect(bridge.browserScreenshot).toHaveBeenCalledWith(browserViewId(CONV, "tab-second"));
    expect(hook.result.current.selected).toBe("tab-second");
  });

  it("targets a nonselected own tab and rejects foreign, closed, and raw view IDs", async () => {
    const bridge = installBridge({ browserClose: vi.fn().mockResolvedValue({ ok: true }) });
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-one", "tab-two"],
      selectedBrowserId: "tab-two",
    });
    const hook = renderWithTabs();

    await sendClaimed("click", { tab_id: "tab-one", selector: "button.save" }, 1);
    expect(bridge.browserExecute).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-one"),
      expect.any(String),
    );
    expect(hook.result.current.selected).toBe("tab-two");
    await act(async () => {
      await hook.result.current.close("tab-one");
    });
    writeSessionWorkspaceState("other-session", {
      openBrowsers: ["foreign-tab"],
      selectedBrowserId: "foreign-tab",
    });

    for (const tabId of ["tab-one", browserViewId(CONV, "tab-one"), "foreign-tab", "bad:tab"]) {
      // oxlint-disable-next-line no-await-in-loop -- Each action result has its own assertion.
      const outcome = await sendClaimed(
        "navigate",
        { tab_id: tabId, url: "https://example.com" },
        resultCount() + 1,
      );
      expect(outcome.error).toMatch(/tab_id is not an open tab in this session/);
    }
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(hook.result.current.tabs).toEqual(["tab-two"]);
    expect(hook.result.current.selected).toBe("tab-two");
  });

  it("surfaces an explicitly targeted screenshot tab", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-one", "tab-two"],
      selectedBrowserId: "tab-two",
    });
    const hook = renderWithTabs();
    const result = await sendClaimed("screenshot", { tab_id: "tab-one" }, 1);
    expect(result).toMatchObject({ ok: true, data: { tab_id: "tab-one" } });
    expect(bridge.browserScreenshot).toHaveBeenCalledWith(browserViewId(CONV, "tab-one"));
    expect(hook.result.current.selected).toBe("tab-one");
  });

  it("persists a background session's claimed target without its tab hook mounted", async () => {
    const background = "conv_background_A";
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(background, {
      openBrowsers: ["tab-one", "tab-two"],
      selectedBrowserId: "tab-two",
    });
    renderRelay(CONV);
    act(() =>
      emitBrowserActionRequest(
        actionEvent("navigate", { tab_id: "tab-one", url: "https://example.com/a" }),
        background,
      ),
    );
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      browserViewId(background, "tab-one"),
      "https://example.com/a",
      undefined,
      { force: true, agent: true },
    );
    expect(readSessionWorkspaceState(background).selectedBrowserId).toBe("tab-one");
  });

  it("rejects missing snapshot identity and a ref from another tab", async () => {
    const bridge = installBridge({
      browserExecute: vi.fn(async (viewId: string, js: string) => {
        (window as unknown as { __omni_snapshot_id__?: string }).__omni_snapshot_id__ =
          viewId === browserViewId(CONV, "tab-one") ? "snap-one" : "snap-two";
        try {
          const value: unknown = window.eval(js);
          return { ok: true, result: typeof value === "string" ? value : JSON.stringify(value) };
        } catch (error) {
          return { ok: false, error: (error as Error).message };
        }
      }),
    });
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-one", "tab-two"],
      selectedBrowserId: "tab-two",
    });
    renderWithTabs();

    const missing = await sendClaimed("click", { tab_id: "tab-one", ref: 1 }, 1);
    expect(missing.error).toMatch(/snapshot_id is required with ref/);
    expect(bridge.browserExecute).not.toHaveBeenCalled();

    const wrong = await sendClaimed(
      "click",
      {
        tab_id: "tab-two",
        ref: 1,
        snapshot_id: "snap-one",
      },
      2,
    );
    expect(wrong.error).toMatch(/snapshot snap-one was superseded by snap-two/);
    expect(bridge.browserExecute).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-two"),
      expect.any(String),
    );
  });

  it("keeps lookup errors clear when Electron hides thrown page exceptions", async () => {
    const generic =
      "Script failed to execute, this normally means an error was thrown. Check the renderer console for the error.";
    let refs: Map<number, { deref: () => Element | undefined }> | undefined;
    const bridge = installBridge({
      browserExecute: vi.fn(async (_viewId: string, js: string) => {
        (window as unknown as { __omni_snapshot_id__?: string }).__omni_snapshot_id__ = "snap-b";
        (window as unknown as { __omni_refs__?: typeof refs }).__omni_refs__ = refs;
        try {
          const value: unknown = window.eval(js);
          return { ok: true, result: typeof value === "string" ? value : JSON.stringify(value) };
        } catch {
          return { ok: false, error: generic };
        }
      }),
    });
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-a", "tab-b"],
      selectedBrowserId: "tab-b",
    });
    renderWithTabs();

    const stale = await sendClaimed("click", { ref: 1, snapshot_id: "snap-a" }, 1);
    expect(stale.error).toBe(
      "snapshot snap-a was superseded by snap-b — call browser_snapshot again",
    );
    expect(bridge.browserExecute).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-b"),
      expect.any(String),
    );

    const noSnapshot = await sendClaimed("click", { ref: 1, snapshot_id: "snap-b" }, 2);
    expect(noSnapshot.error).toBe("no snapshot in this page — call browser_snapshot first");
    refs = new Map();
    const absentRef = await sendClaimed("click", { ref: 1, snapshot_id: "snap-b" }, 3);
    expect(absentRef.error).toBe("ref 1 not in snapshot — call browser_snapshot again");
    refs.set(1, { deref: () => undefined });
    const collected = await sendClaimed("type", { ref: 1, snapshot_id: "snap-b", text: "hi" }, 4);
    expect(collected.error).toBe("ref 1 was garbage-collected — call browser_snapshot again");
    const missing = await sendClaimed("type", { selector: "#absent", text: "hello" }, 5);
    expect(missing.error).toBe("selector not found: #absent");

    const button = document.createElement("button");
    button.id = "present";
    button.scrollIntoView = vi.fn();
    const onClick = vi.fn();
    button.addEventListener("click", onClick);
    document.body.append(button);
    const success = await sendClaimed("click", { selector: "#present" }, 6);
    expect(success).toEqual({ ok: true });
    expect(onClick).toHaveBeenCalledTimes(1);
    button.remove();
  });

  it("opens the reserved tab for the first navigation and reports a missing browser view", async () => {
    const bridge = installBridge({
      browserScreenshot: vi.fn().mockResolvedValue({ ok: false, error: "No browser view" }),
    });
    authenticatedFetch.mockResolvedValue(WON);
    const hook = renderWithTabs();
    const missing = await sendClaimed("screenshot", {}, 1);
    expect(missing).toEqual({ ok: false, error: "No browser view" });
    expect(hook.result.current.tabs).toEqual([]);
    const opened = await sendClaimed("navigate", { url: "https://example.com/start" }, 2);
    expect(opened).toMatchObject({ ok: true, data: { tab_id: "agent-browser" } });
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "https://example.com/start",
      undefined,
      { force: true, agent: true },
    );
    expect(hook.result.current.tabs).toEqual(["agent-browser"]);
    expect(hook.result.current.selected).toBe("agent-browser");
  });

  it("restores a legacy reserved view when capturing it", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "https://example.com" }),
    });
    authenticatedFetch.mockResolvedValue(WON);
    const hook = renderWithTabs();
    const outcome = await sendClaimed("screenshot", {}, 1);
    expect(outcome).toMatchObject({ ok: true, data: { tab_id: "agent-browser" } });
    expect(bridge.browserScreenshot).toHaveBeenCalledWith(CONV);
    expect(hook.result.current.tabs).toEqual(["agent-browser"]);
  });

  it("reuses the selected tab, creates a new one on request, and keeps the resolved target after selection changes", async () => {
    let resolveHost!: (session: unknown) => void;
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHost = resolve;
        }),
    );
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["tab-one", "tab-two"],
      selectedBrowserId: "tab-one",
    });
    const hook = renderWithTabs();
    act(() =>
      emitBrowserActionRequest(
        actionEvent("navigate", { url: "https://example.com/a" }, "nav-one"),
        CONV,
      ),
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    act(() => hook.result.current.select("tab-two"));
    resolveHost({ id: CONV, hostId: null, parentSessionId: null });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(hook.result.current.selected).toBe("tab-one");
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      browserViewId(CONV, "tab-one"),
      "https://example.com/a",
      undefined,
      { force: true, agent: true },
    );

    const next = await sendClaimed("navigate", { url: "https://example.com/b", new_tab: true }, 2);
    const newId = (next.data as { tab_id: string }).tab_id;
    expect(newId).not.toBe("tab-one");
    expect(newId).not.toBe("tab-two");
    expect(hook.result.current.selected).toBe(newId);
    expect(bridge.browserOpenOrNavigate).toHaveBeenLastCalledWith(
      browserViewId(CONV, newId),
      "https://example.com/b",
      undefined,
      { force: true, agent: true },
    );
    const invalid = await sendClaimed(
      "navigate",
      {
        tab_id: "tab-one",
        new_tab: true,
        url: "https://example.com/c",
      },
      3,
    );
    expect(invalid.error).toMatch(/cannot be used together/);
    expect(hook.result.current.tabs).toHaveLength(3);
  });

  it("does not change tabs or surface a losing claim", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValue(jsonResponse({ claimed: false }));
    writeSessionWorkspaceState(CONV, { openBrowsers: ["tab-one"], selectedBrowserId: "tab-one" });
    const hook = renderWithTabs();
    act(() =>
      emitBrowserActionRequest(
        actionEvent("navigate", {
          url: "https://example.com",
          new_tab: true,
        }),
        CONV,
      ),
    );
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(hook.result.current.tabs).toEqual(["tab-one"]);
    expect(hook.result.current.selected).toBe("tab-one");
  });

  it("does not create a new tab when its claim resolves after relay unmount", async () => {
    let resolveClaim!: (response: Response) => void;
    authenticatedFetch
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveClaim = resolve;
          }),
      )
      .mockResolvedValue(jsonResponse({}));
    installBridge();
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", {
        url: "https://example.com",
        new_tab: true,
      }),
      CONV,
    );
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    hook.unmount();
    resolveClaim(WON);
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(readSessionWorkspaceState(CONV).openBrowsers ?? []).toEqual([]);
    expect(readSessionWorkspaceState(CONV).selectedBrowserId).toBeUndefined();
  });

  it("does not create a tab after host lookup outlives the relay", async () => {
    let resolveHost!: (session: unknown) => void;
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHost = resolve;
        }),
    );
    authenticatedFetch.mockResolvedValue(WON);
    installBridge();
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", {
        url: "https://example.com",
        new_tab: true,
      }),
      CONV,
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    hook.unmount();
    resolveHost({ id: CONV, hostId: null, parentSessionId: null });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(readSessionWorkspaceState(CONV).openBrowsers ?? []).toEqual([]);
    expect(readSessionWorkspaceState(CONV).selectedBrowserId).toBeUndefined();
  });

  it("does not create the reserved tab after host lookup outlives the relay", async () => {
    let resolveHost!: (session: unknown) => void;
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHost = resolve;
        }),
    );
    authenticatedFetch.mockResolvedValue(WON);
    installBridge();
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("navigate", { url: "https://example.com" }), CONV);
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    hook.unmount();
    resolveHost({ id: CONV, hostId: null, parentSessionId: null });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(readSessionWorkspaceState(CONV).openBrowsers ?? []).toEqual([]);
    expect(readSessionWorkspaceState(CONV).selectedBrowserId).toBeUndefined();
  });

  it("does not restore a legacy view after metadata lookup outlives the relay", async () => {
    let resolveView!: (view: { exists: boolean }) => void;
    authenticatedFetch.mockResolvedValue(WON);
    installBridge({
      browserHasView: vi.fn(
        () =>
          new Promise((resolve) => {
            resolveView = resolve;
          }),
      ),
    });
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);
    await vi.waitFor(() => expect(resolveView).toBeDefined());
    hook.unmount();
    resolveView({ exists: true });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(readSessionWorkspaceState(CONV).openBrowsers ?? []).toEqual([]);
    expect(readSessionWorkspaceState(CONV).selectedBrowserId).toBeUndefined();
  });

  it("does not recreate a target closed while host lookup is pending", async () => {
    let resolveHost!: (session: unknown) => void;
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHost = resolve;
        }),
    );
    authenticatedFetch.mockResolvedValue(WON);
    const bridge = installBridge({ browserClose: vi.fn().mockResolvedValue({ ok: true }) });
    writeSessionWorkspaceState(CONV, { openBrowsers: ["tab-one"], selectedBrowserId: "tab-one" });
    const hook = renderWithTabs();
    emitBrowserActionRequest(actionEvent("navigate", { url: "https://example.com" }), CONV);
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    await act(async () => {
      await hook.result.current.close("tab-one");
    });
    resolveHost({ id: CONV, hostId: null, parentSessionId: null });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(readSessionWorkspaceState(CONV).openBrowsers).toEqual([]);
    expect((postedResult().result as { error: string }).error).toMatch(/no longer open/);
  });

  it("does not reopen a tab while its native close reply is pending", async () => {
    let resolveHost!: (session: unknown) => void;
    let finishClose!: (result: { ok: boolean }) => void;
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHost = resolve;
        }),
    );
    authenticatedFetch.mockResolvedValue(WON);
    const bridge = installBridge({
      browserClose: vi.fn(
        () =>
          new Promise((resolve) => {
            finishClose = resolve;
          }),
      ),
    });
    writeSessionWorkspaceState(CONV, { openBrowsers: ["tab-one"], selectedBrowserId: "tab-one" });
    const hook = renderWithTabs();
    emitBrowserActionRequest(actionEvent("navigate", { url: "https://example.com" }), CONV);
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    const pendingClose = hook.result.current.close("tab-one");
    expect(bridge.browserClose).toHaveBeenCalledWith(browserViewId(CONV, "tab-one"));
    expect(readSessionWorkspaceState(CONV).openBrowsers).toEqual(["tab-one"]);
    resolveHost({ id: CONV, hostId: null, parentSessionId: null });
    await vi.waitFor(() => expect(resultCount()).toBe(1));
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect((postedResult().result as { error: string }).error).toMatch(/closing/);
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });
    expect(readSessionWorkspaceState(CONV).openBrowsers).toEqual([]);
  });

  it("rejects a selected closing tab but allows an explicit new tab", async () => {
    let finishClose!: (result: { ok: boolean }) => void;
    authenticatedFetch.mockResolvedValue(WON);
    const bridge = installBridge({
      browserClose: vi.fn(
        () =>
          new Promise((resolve) => {
            finishClose = resolve;
          }),
      ),
    });
    writeSessionWorkspaceState(CONV, { openBrowsers: ["tab-one"], selectedBrowserId: "tab-one" });
    const hook = renderWithTabs();
    const pendingClose = hook.result.current.close("tab-one");
    const implicit = await sendClaimed("navigate", { url: "https://example.com/a" }, 1);
    expect(implicit.error).toMatch(/closing/);
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    const explicit = await sendClaimed(
      "navigate",
      {
        url: "https://example.com/b",
        new_tab: true,
      },
      2,
    );
    expect(explicit.ok).toBe(true);
    const newId = (explicit.data as { tab_id: string }).tab_id;
    expect(newId).not.toBe("tab-one");
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });
    expect(readSessionWorkspaceState(CONV).openBrowsers).toEqual([newId]);
  });

  it("lists loaded and blank own tabs even when the selected blank tab has no view", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn(async (id: string) =>
        id === browserViewId(CONV, "loaded")
          ? { exists: true, title: "Loaded", url: "https://example.com" }
          : { exists: false },
      ),
      browserExecute: vi.fn(async (id: string) =>
        id === browserViewId(CONV, "loaded")
          ? {
              ok: true,
              result: JSON.stringify({ snapshot_id: "s-loaded", tree: "- heading [ref=1]" }),
            }
          : { ok: false, error: "No browser view" },
      ),
    });
    authenticatedFetch.mockResolvedValue(WON);
    writeSessionWorkspaceState(CONV, {
      openBrowsers: ["loaded", "blank"],
      selectedBrowserId: "blank",
    });
    renderWithTabs();
    const unavailable = await sendClaimed("snapshot", {}, 1);
    expect(unavailable).toMatchObject({
      ok: false,
      error: "No browser view",
      data: {
        tab_id: "blank",
        tabs: [
          { tab_id: "loaded", title: "Loaded", url: "https://example.com", selected: false },
          { tab_id: "blank", title: "", url: "", selected: true },
        ],
      },
    });
    const loaded = await sendClaimed("snapshot", { tab_id: "loaded" }, 2);
    expect(loaded).toMatchObject({ ok: true, data: { tab_id: "loaded", snapshot_id: "s-loaded" } });
    expect(bridge.browserExecute).toHaveBeenLastCalledWith(
      browserViewId(CONV, "loaded"),
      expect.any(String),
    );
  });
});

describe("useBrowserAgentRelay — action dispatch", () => {
  beforeEach(() => {
    // Every dispatch test wins the claim, then a benign result POST.
    authenticatedFetch.mockResolvedValue(WON);
  });
  it("derives inherited provenance from the source session, never model args or visible host", async () => {
    const bridge = installBridge();
    setSessionHost(CONV, "local-host");
    setSessionHost("parent", "arca-host");
    setSessionParent("conv_background_A", "parent");
    getSessionSlim.mockImplementation(async (id: string) => ({
      id,
      hostId: id === "parent" ? "arca-host" : null,
      parentSessionId: id === "parent" ? null : "parent",
    }));
    await runAction(
      actionEvent("navigate", {
        url: "http://localhost:5173",
        sourceHostId: "forged",
        isArca: true,
      }),
      { source: "conv_background_A" },
    );
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual(["conv_background_A", "parent"]);
  });

  it("loads a child's own host before granting eligibility from a parent-list hint", async () => {
    const bridge = installBridge();
    setSessionHost("parent", "arca-host");
    setSessionParent("conv_background_A", "parent");
    getSessionSlim.mockResolvedValue({
      id: "conv_background_A",
      hostId: "other-host",
      parentSessionId: "parent",
    });
    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }), {
      source: "conv_background_A",
    });
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "other-host" },
    );
  });

  it("uses an authoritative cached child snapshot without refetching or trusting parent hints", async () => {
    const bridge = installBridge();
    const client = new QueryClient();
    client.setQueryData(["session", "conv_background_A"], {
      id: "conv_background_A",
      hostId: "other-host",
      parentSessionId: "parent",
    });
    setSessionHost("parent", "arca-host");
    setSessionParent("conv_background_A", "parent");
    renderRelay(CONV, client);
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173" }),
      "conv_background_A",
    );
    await vi.waitFor(() => expect(postedResult().result).toHaveProperty("ok", true));
    expect(getSessionSlim).not.toHaveBeenCalled();
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "other-host" },
    );
  });

  it("reads an unloaded intermediate's own host instead of borrowing a distant Arca hint", async () => {
    const bridge = installBridge();
    setSessionParent(CONV, "middle");
    setSessionParent("middle", "root");
    setSessionHost("root", "arca-host");
    getSessionSlim.mockImplementation(async (id: string) => ({
      id,
      hostId: id === "middle" ? "other-host" : null,
      parentSessionId: id === CONV ? "middle" : "root",
    }));
    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }));
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual([CONV, "middle"]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "other-host" },
    );
  });

  it("inherits only through authoritative hostless hops and reuses cached ancestor snapshots", async () => {
    const bridge = installBridge();
    const client = new QueryClient();
    client.setQueryData(["session", "middle"], {
      id: "middle",
      hostId: null,
      parentSessionId: "root",
    });
    client.setQueryData(["session", "root"], {
      id: "root",
      hostId: "arca-host",
      parentSessionId: null,
    });
    const fetchQuery = vi.spyOn(client, "fetchQuery");
    setSessionParent(CONV, "middle");
    setSessionParent("middle", "root");
    setSessionHost("root", "arca-host");
    getSessionSlim.mockResolvedValue({ id: CONV, hostId: null, parentSessionId: "middle" });
    renderRelay(CONV, client);
    emitBrowserActionRequest(actionEvent("navigate", { url: "http://localhost:5173" }), CONV);
    await vi.waitFor(() => expect(postedResult().result).toHaveProperty("ok", true));
    expect(fetchQuery.mock.calls.map(([query]) => query.queryKey)).toEqual([
      ["session", CONV],
      ["session", "middle"],
      ["session", "root"],
    ]);
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual([CONV]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
  });

  it.each(["lookup failure", "cycle"])(
    "does not borrow a distant host hint when authoritative ancestry ends in %s",
    async (failure) => {
      const bridge = installBridge();
      const client = new QueryClient();
      client.setQueryData(["session", "root"], {
        id: "root",
        hostId: "arca-host",
        parentSessionId: null,
      });
      setSessionParent(CONV, "middle");
      setSessionParent("middle", "root");
      setSessionHost("root", "arca-host");
      getSessionSlim.mockImplementation(async (id: string) => {
        if (id === "middle" && failure === "lookup failure") {
          throw new Error("ancestor unavailable");
        }
        return { id, hostId: null, parentSessionId: id === CONV ? "middle" : CONV };
      });
      renderRelay(CONV, client);
      emitBrowserActionRequest(actionEvent("navigate", { url: "http://localhost:5173" }), CONV);
      await vi.waitFor(() => expect(postedResult().result).toHaveProperty("ok", true));
      expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual([CONV, "middle"]);
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
        CONV,
        "http://localhost:5173",
        undefined,
        { force: true, agent: true },
      );
    },
  );

  it.each(["http://localhost:5173", "https://example.com"])(
    "does not authorize an inherited hint after the source lookup fails (%s)",
    async (url) => {
      const bridge = installBridge();
      setSessionHost("parent", "arca-host");
      setSessionParent("conv_background_A", "parent");
      getSessionSlim.mockRejectedValue(new Error("source unavailable"));
      await runAction(actionEvent("navigate", { url }), { source: "conv_background_A" });
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
        "conv_background_A",
        url,
        undefined,
        { force: true, agent: true },
      );
      expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual(["conv_background_A"]);
    },
  );

  it("fetches missing source metadata and leaves failed/unknown resolution unprivileged", async () => {
    const bridge = installBridge();
    getSessionSlim.mockRejectedValue(new Error("unknown session"));
    await runAction(actionEvent("navigate", { url: "http://localhost:5173", isArca: true }));
    expect(getSessionSlim).toHaveBeenCalledWith(CONV);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "http://localhost:5173",
      undefined,
      { force: true, agent: true },
    );
  });

  it("keeps source-host resolution alive across visible-session switches", async () => {
    const bridge = installBridge();
    let resolveHost!: (source: unknown) => void;
    getSessionSlim.mockImplementation((id: string) =>
      id === "parent"
        ? new Promise((resolve) => {
            resolveHost = resolve;
          })
        : Promise.resolve({ id, hostId: null, parentSessionId: "parent" }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173" }),
      "conv_background_A",
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith("parent"));
    hook.rerender({ id: "conv_visible_B" });
    setSessionHost("conv_visible_B", "local-host");
    resolveHost({ id: "parent", hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: true,
        data: { final_url: "http://localhost:5173", tab_id: "agent-browser" },
      }),
    );
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual(["conv_background_A", "parent"]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
    expect(authenticatedFetch.mock.calls[1][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_result/",
    );
  });

  it("does not dispatch a stale metadata resolution after relay unmount", async () => {
    const bridge = installBridge();
    let resolve!: (source: unknown) => void;
    getSessionSlim.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("navigate", { url: "http://localhost:5173" }), CONV);
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    hook.unmount();
    resolve({ id: CONV, hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: false,
        error: "browser relay context changed",
      }),
    );
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("keeps the authoritative source lookup alive across visible-session switches", async () => {
    const bridge = installBridge();
    let resolve!: (source: unknown) => void;
    getSessionSlim.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173" }),
      "conv_background_A",
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith("conv_background_A"));
    hook.rerender({ id: "conv_visible_B" });
    setSessionHost("conv_visible_B", "local-host");
    resolve({ id: "conv_background_A", hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() => expect(postedResult().result).toHaveProperty("ok", true));
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
  });

  it("navigate: reports the final_url and marks it agent+force", async () => {
    installBridge();
    await runAction(actionEvent("navigate", { url: "https://myhost/page" }));
    expect((postedResult().result as { data: { final_url: string } }).data.final_url).toBe(
      "https://myhost/page",
    );
  });

  it("navigate: empty url is rejected before touching the bridge", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("navigate", { url: "" }));
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect((postedResult().result as { ok: boolean; error: string }).error).toMatch(
      /url is required/,
    );
  });

  it("navigate: surfaces the bridge error when the registry rejects", async () => {
    installBridge({
      browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: false, error: "blocked host" }),
    });
    await runAction(actionEvent("navigate", { url: "https://x" }));
    expect((postedResult().result as { error: string }).error).toBe("blocked host");
  });

  it("screenshot: returns the data_url from the bridge", async () => {
    installBridge();
    await runAction(actionEvent("screenshot"));
    expect((postedResult().result as { data_url: string }).data_url).toBe(
      "data:image/png;base64,AAA",
    );
  });

  it("screenshot: reports 'No browser open' when the bridge has no image", async () => {
    installBridge({ browserScreenshot: vi.fn().mockResolvedValue({ ok: false }) });
    await runAction(actionEvent("screenshot"));
    expect((postedResult().result as { error: string }).error).toMatch(/No browser open/);
  });

  it("snapshot: parses the executed JSON tree", async () => {
    const bridge = installBridge({
      browserExecute: vi
        .fn()
        .mockResolvedValue({ ok: true, result: JSON.stringify({ snapshot_id: "s1", tree: "x" }) }),
    });
    await runAction(actionEvent("snapshot"));
    // The snapshot JS is the fixed SNAPSHOT_JS constant (walks the DOM).
    expect(executedJs(bridge)).toContain("__omni_refs__");
    expect((postedResult().result as { data: { snapshot_id: string } }).data.snapshot_id).toBe(
      "s1",
    );
  });

  it("snapshot: reports a parse error on non-JSON output", async () => {
    installBridge({ browserExecute: vi.fn().mockResolvedValue({ ok: true, result: "not json" }) });
    await runAction(actionEvent("snapshot"));
    expect((postedResult().result as { error: string }).error).toMatch(/snapshot parse failed/);
  });

  it("click by ref: validates snapshot_id and clicks the resolved element", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("click", { ref: 7, snapshot_id: "snap-9" }));
    const js = executedJs(bridge);
    expect(js).toContain('__omni_snapshot_id__ !== "snap-9"');
    expect(js).toContain("__omni_refs__");
    expect(js).toContain("el.click()");
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("click by selector: resolves via querySelector (neutral selector, JSON-escaped)", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("click", { selector: "button.submit" }));
    const js = executedJs(bridge);
    expect(js).toContain('document.querySelector("button.submit")');
  });

  it("type: sets the value via the native setter and dispatches input/change", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("type", { ref: 3, snapshot_id: "snap-3", text: "hello" }));
    const js = executedJs(bridge);
    expect(js).toContain('"hello"'); // text JSON-escaped into the payload
    expect(js).toContain("input");
    expect(js).toContain("change");
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("click: surfaces the in-page execute error", async () => {
    installBridge({
      browserExecute: vi.fn().mockResolvedValue({ ok: false, error: "selector not found: x" }),
    });
    await runAction(actionEvent("click", { selector: "x" }));
    expect((postedResult().result as { error: string }).error).toBe("selector not found: x");
  });

  it("unknown action is reported, not dispatched", async () => {
    installBridge();
    await runAction(actionEvent("teleport"));
    expect((postedResult().result as { error: string }).error).toMatch(
      /Unknown browser action: teleport/,
    );
  });

  it("missing bridge method → 'does not support the browser pane'", async () => {
    installBridge({ browserExecute: undefined });
    await runAction(actionEvent("snapshot"));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("type: missing execute bridge → 'does not support the browser pane'", async () => {
    installBridge({ browserExecute: undefined });
    await runAction(actionEvent("type", { ref: 1, snapshot_id: "snap-1", text: "x" }));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("navigate: missing open bridge → 'does not support the browser pane'", async () => {
    installBridge({ browserOpenOrNavigate: undefined });
    await runAction(actionEvent("navigate", { url: "https://x" }));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("dispatch surfaces a thrown in-page/IPC error as {ok:false} (outer catch)", async () => {
    installBridge({
      browserExecute: vi.fn().mockRejectedValue(new Error("execute blew up")),
    });
    await runAction(actionEvent("click", { selector: "x" }));
    expect((postedResult().result as { ok: boolean; error: string }).error).toBe("execute blew up");
  });

  it("screenshot: a rejected capture IPC call still posts {ok:false} (outer catch)", async () => {
    installBridge({
      browserScreenshot: vi.fn().mockRejectedValue(new Error("capture IPC blew up")),
    });
    await runAction(actionEvent("screenshot"));
    expect((postedResult().result as { ok: boolean; error: string }).error).toBe(
      "capture IPC blew up",
    );
  });
});

describe("useBrowserAgentRelay — hidden pane handling", () => {
  beforeEach(() => {
    authenticatedFetch.mockResolvedValue(WON);
  });

  it("snapshot: reports the hidden-pane error when the page has no layout size", async () => {
    const innerWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
    const innerHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 0 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 0 });
    try {
      const bridge = installBridge({
        browserExecute: vi.fn(async (_id: string, js: string) => ({
          ok: true,
          result: window.eval(js),
        })),
      });
      await runAction(actionEvent("snapshot"));
      expect(bridge.browserExecute).toHaveBeenCalled();
      expect(postedResult().result).toEqual({
        ok: false,
        error:
          "the page has no layout size because its browser pane is hidden; open this session's Browser tab and retry",
        data: { tab_id: "agent-browser", tabs: [] },
      });
    } finally {
      Object.defineProperty(window, "innerWidth", innerWidth!);
      Object.defineProperty(window, "innerHeight", innerHeight!);
    }
  });

  it("snapshot: succeeds when the page has a layout size", async () => {
    const bridge = installBridge({
      browserExecute: vi.fn(async (_id: string, js: string) => ({
        ok: true,
        result: window.eval(js),
      })),
    });
    await runAction(actionEvent("snapshot"));
    expect(bridge.browserExecute).toHaveBeenCalled();
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("screenshot: retries while the pane is hidden and returns the data URL once surfaced", async () => {
    vi.useFakeTimers();
    try {
      installBridge({
        browserScreenshot: vi
          .fn()
          .mockResolvedValueOnce({ ok: false, noSurface: true })
          .mockResolvedValueOnce({ ok: true, dataUrl: "data:image/png;base64," })
          .mockResolvedValueOnce({ ok: true, dataUrl: "data:image/png;base64,REAL" }),
      });
      authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));
      renderRelay();
      emitBrowserActionRequest(actionEvent("screenshot"), CONV);
      await vi.advanceTimersByTimeAsync(500);
      expect(postedResult().result).toEqual({
        ok: true,
        data_url: "data:image/png;base64,REAL",
        data: { tab_id: "agent-browser" },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("screenshot: gives up with the pane-hidden message when the shell keeps returning an empty image", async () => {
    vi.useFakeTimers();
    try {
      const bridge = installBridge({
        browserScreenshot: vi
          .fn()
          .mockResolvedValue({ ok: true, dataUrl: "data:image/png;base64," }),
      });
      renderRelay();
      emitBrowserActionRequest(actionEvent("screenshot"), CONV);
      await vi.advanceTimersByTimeAsync(3500);
      expect(bridge.browserScreenshot.mock.calls.length).toBeGreaterThan(1);
      expect(postedResult().result).toEqual({
        ok: false,
        error:
          "screenshot needs this session's browser pane on screen and it is hidden (another session or panel tab is showing); browser_snapshot, browser_click and browser_type work while it is hidden",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("screenshot: returns a non-surface failure immediately without retrying", async () => {
    const bridge = installBridge({
      browserScreenshot: vi.fn().mockResolvedValue({ ok: false, error: "capture blew up" }),
    });
    await runAction(actionEvent("screenshot"));
    expect(bridge.browserScreenshot).toHaveBeenCalledTimes(1);
    expect(postedResult().result).toEqual({ ok: false, error: "capture blew up" });
  });
});

describe("useBrowserAgentRelay — result POST resilience", () => {
  it("swallows a failing result POST (best-effort; server timeout covers it)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installBridge();
    // Claim wins, but the result POST rejects — must not throw out of the handler.
    authenticatedFetch
      .mockResolvedValueOnce(WON)
      .mockRejectedValueOnce(new Error("result POST network error"));

    renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);

    await vi.waitFor(() => {
      // Both the claim and the (failed) result POST were attempted.
      expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    });
    // The postResult catch logged rather than throwing.
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
    warn.mockRestore();
  });

  it("does nothing when the shell exposes no bridge at handler time", async () => {
    // isElectronShell() is mocked true (hook registers), but omnigentDesktop is
    // absent — getBrowserDesktop() returns null, so the handler bails before claim.
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;

    renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);
    await Promise.resolve();
    await Promise.resolve();

    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});
