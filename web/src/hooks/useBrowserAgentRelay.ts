/** Browser MCP relay hook — runs agent-issued browser actions against the
 *  Electron WebContentsView via a claim-first protocol: on a
 *  `browser.action_request` SSE event every renderer races to CLAIM the action
 *  (atomic CAS on the server), and only the `{claimed:true, claim_token}` winner
 *  dispatches to `window.omnigentDesktop.browser*` and POSTs the result back
 *  with its token — so multiple windows can't double-execute.
 *  Gated on `supportsBrowser()`: without a WebContentsView the hook registers
 *  nothing and actions time out cleanly (no headless fallback). An older
 *  desktop build that predates the `browser*` bridge is treated as unsupported,
 *  so the relay never claims an action it couldn't fulfill. */
import { useEffect } from "react";
import { buildBrowserActionScriptForExecution } from "../../electron/src/browserActionScript.mjs";
import {
  emitBrowserActionClaimed,
  onBrowserActionRequest,
  surfacesBrowserPane,
} from "@/lib/browserActionBus";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { supportsBrowser } from "@/lib/nativeBridge";
import { authenticatedFetch } from "@/lib/identity";
import { useQueryClient } from "@tanstack/react-query";
import { sessionRoutingHostId, setSessionHost, setSessionParent } from "@/lib/sessionHost";
import { getSessionSlim } from "@/lib/sessionsApi";
import { isArcaHost, readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import type { Host } from "@/hooks/useHosts";
import type { Session } from "@/lib/types";
import {
  AGENT_BROWSER_TAB_ID,
  browserViewId,
  createBrowserTab,
  isBrowserTabClosing,
  openAgentBrowserTab,
  readBrowserTabsState,
  selectBrowserTab,
  validBrowserTabId,
} from "./useBrowserTabs";

/** Subset of `window.omnigentDesktop` the relay calls (typed locally, not via
 *  nativeBridge). All optional — an older shell may predate the feature, so the
 *  relay feature-detects before calling. */
interface BrowserDesktopBridge {
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds?: unknown,
    opts?: {
      force?: boolean;
      agent?: boolean;
      sourceHostId?: string;
      rememberedArcaHostId?: string;
      sourceIsArcaHost?: boolean;
    },
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
  browserScreenshot?: (
    conversationId: string,
  ) => Promise<{ ok: boolean; dataUrl?: string; error?: string; noSurface?: boolean }>;
  /** @deprecated Compatibility with pre-typed desktops until v0.21. */
  browserExecute?: (
    conversationId: string,
    script: string,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
  browserHasView?: (viewId: string) => Promise<{
    exists: boolean;
    url?: string;
    title?: string;
  }>;
  browserPerformAction?: (
    conversationId: string,
    action: "snapshot" | "click" | "type",
    args?: Record<string, unknown>,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
}

function isLoopbackPreview(url: string): boolean {
  try {
    const { hostname } = new URL(url);
    return (
      hostname === "localhost" ||
      hostname.endsWith(".localhost") ||
      hostname === "127.0.0.1" ||
      hostname === "[::1]"
    );
  } catch {
    return false;
  }
}

function getBrowserDesktop(): BrowserDesktopBridge | null {
  if (!supportsBrowser()) return null;
  const w = window as unknown as { omnigentDesktop?: BrowserDesktopBridge };
  return w.omnigentDesktop ?? null;
}

/** The shape the relay POSTs back to the server as the action `result`. Normalized
 *  so the backend/tool layer can branch on `ok` alone. */
interface ActionResult {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
  data_url?: string;
}

// A hidden view cannot be captured; poll while the surfacing action opens the
// session's Browser tab (older shells even report an empty PNG as success).
const SCREENSHOT_SURFACE_RETRY_MS = 250;
const SCREENSHOT_SURFACE_TIMEOUT_MS = 3000;
const EMPTY_SCREENSHOT_DATA_URL = "data:image/png;base64,";
const SCREENSHOT_PANE_HIDDEN_ERROR =
  "screenshot needs this session's browser pane on screen and it is hidden (another session or panel tab is showing); browser_snapshot, browser_click and browser_type work while it is hidden";

interface BrowserTarget {
  tabId: string;
  viewId: string;
  kind: "open" | "new" | "reserved" | "legacy";
}

/** Resolve a tool target only from this session's persisted tab IDs. */
function resolveBrowserTarget(
  conversationId: string,
  action: string,
  args: Record<string, unknown>,
): BrowserTarget | ActionResult {
  const requested = args.tab_id;
  const newTab = args.new_tab;
  if (newTab !== undefined && (action !== "navigate" || typeof newTab !== "boolean")) {
    return { ok: false, error: "new_tab is only valid for browser_navigate" };
  }
  if (requested !== undefined && newTab === true) {
    return { ok: false, error: "tab_id and new_tab cannot be used together" };
  }
  const state = readBrowserTabsState(conversationId);
  let target: BrowserTarget;
  if (requested !== undefined) {
    if (
      typeof requested !== "string" ||
      !validBrowserTabId(requested) ||
      !state.tabs.includes(requested)
    ) {
      return { ok: false, error: "tab_id is not an open tab in this session" };
    }
    target = { tabId: requested, viewId: browserViewId(conversationId, requested), kind: "open" };
  } else if (newTab === true) {
    const tabId = crypto.randomUUID();
    target = { tabId, viewId: browserViewId(conversationId, tabId), kind: "new" };
  } else if (state.selected) {
    target = {
      tabId: state.selected,
      viewId: browserViewId(conversationId, state.selected),
      kind: "open",
    };
  } else {
    // Older shells can still have a reserved session view without a saved soft tab.
    target = {
      tabId: AGENT_BROWSER_TAB_ID,
      viewId: conversationId,
      kind: action === "navigate" ? "reserved" : "legacy",
    };
  }
  if (isBrowserTabClosing(conversationId, target.tabId)) {
    return { ok: false, error: "tab_id is closing in this session" };
  }
  return target;
}

async function tabMetadata(
  conversationId: string,
  desktop: BrowserDesktopBridge,
  target: BrowserTarget,
  pageExists = false,
): Promise<{ tab_id: string; title: string; url: string; selected: boolean }[]> {
  const state = readBrowserTabsState(conversationId);
  const tabIds =
    pageExists && !state.tabs.includes(target.tabId) ? [...state.tabs, target.tabId] : state.tabs;
  return Promise.all(
    tabIds.map(async (tabId) => {
      let metadata: { exists: boolean; url?: string; title?: string } | undefined;
      try {
        metadata = await desktop.browserHasView?.(browserViewId(conversationId, tabId));
      } catch {
        // A metadata lookup must not discard an otherwise valid page snapshot.
      }
      return {
        tab_id: tabId,
        title: metadata?.exists ? (metadata.title ?? "") : "",
        url: metadata?.exists ? (metadata.url ?? "") : "",
        selected: state.selected === tabId || (state.selected === null && tabId === target.tabId),
      };
    }),
  );
}

/** Use the main-owned typed action and the legacy typed decoder on older desktops. */
async function performAction(
  desktop: BrowserDesktopBridge,
  viewId: string,
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
) {
  if (desktop.browserPerformAction) {
    return desktop.browserPerformAction(viewId, action, args);
  }
  if (desktop.browserExecute) {
    return desktop.browserExecute(viewId, buildBrowserActionScriptForExecution(action, args));
  }
  return { ok: false, error: "this desktop shell does not support the browser pane" };
}

function lookupError(result: unknown): string | null {
  if (typeof result !== "string" || !result.startsWith('{"browser_error":')) return null;
  try {
    const parsed = JSON.parse(result) as { browser_error?: unknown };
    return typeof parsed.browser_error === "string" ? parsed.browser_error : null;
  } catch {
    return null;
  }
}

/** Execute one claimed action against the conversation's WebContentsView.
 *  `conversationId` targets the right view; `desktop` is the (feature-detected)
 *  bridge. Returns a normalized `ActionResult` — never throws (the outer catch
 *  converts any in-page/IPC error to `{ok:false, error}`). */
async function dispatch(
  conversationId: string,
  target: BrowserTarget,
  action: string,
  args: Record<string, unknown>,
  desktop: BrowserDesktopBridge,
  sourceHostId: string | null,
  rememberedArcaHostId: string | null,
  sourceIsArcaHost: boolean,
): Promise<ActionResult> {
  try {
    const pageArgs = { ...args };
    delete pageArgs.tab_id;
    delete pageArgs.new_tab;
    switch (action) {
      case "navigate": {
        const url = String(args.url ?? "");
        if (!url) return { ok: false, error: "url is required" };
        if (!desktop.browserOpenOrNavigate) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        // force: honor the explicit agent nav even on same-URL. agent: mark it
        // model-issued so the registry applies the scheme/host allowlist (Risk).
        const r = await desktop.browserOpenOrNavigate(target.viewId, url, undefined, {
          force: true,
          agent: true,
          ...(sourceHostId ? { sourceHostId } : {}),
          ...(rememberedArcaHostId ? { rememberedArcaHostId } : {}),
          ...(sourceIsArcaHost ? { sourceIsArcaHost: true } : {}),
        });
        if (!r?.ok) return { ok: false, error: r?.error ?? "navigate failed" };
        if (
          isLoopbackPreview(url) &&
          sourceHostId &&
          rememberedArcaHostId &&
          sourceHostId !== rememberedArcaHostId
        ) {
          // Main only approves this after consent captures the source host.
          writeArcaHostId(sourceHostId);
        }
        return { ok: true, data: { final_url: url, tab_id: target.tabId } };
      }
      case "screenshot": {
        const screenshot = desktop.browserScreenshot;
        if (!screenshot) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const deadline = Date.now() + SCREENSHOT_SURFACE_TIMEOUT_MS;
        const capture = async (): Promise<ActionResult> => {
          const r = await screenshot(target.viewId);
          const noSurface =
            (!r?.ok && !!r?.noSurface) ||
            (!!r?.ok && (!r.dataUrl || r.dataUrl === EMPTY_SCREENSHOT_DATA_URL));
          if (!noSurface) {
            if (!r?.ok || !r.dataUrl) return { ok: false, error: r?.error ?? "No browser open" };
            return { ok: true, data_url: r.dataUrl, data: { tab_id: target.tabId } };
          }
          if (Date.now() >= deadline) return { ok: false, error: SCREENSHOT_PANE_HIDDEN_ERROR };
          await new Promise<void>((resolve) => {
            setTimeout(resolve, SCREENSHOT_SURFACE_RETRY_MS);
          });
          return capture();
        };
        return await capture();
      }
      case "snapshot": {
        const snapshotFailure = async (error: string): Promise<ActionResult> => ({
          ok: false,
          error,
          data: {
            tab_id: target.tabId,
            tabs: await tabMetadata(conversationId, desktop, target),
          },
        });
        const r = await performAction(desktop, target.viewId, "snapshot");
        if (!r?.ok) return snapshotFailure(r?.error ?? "snapshot failed");
        try {
          const parsed = JSON.parse(r.result ?? "{}") as Record<string, unknown>;
          if (typeof parsed.error === "string") return snapshotFailure(parsed.error);
          return {
            ok: true,
            data: {
              ...parsed,
              tab_id: target.tabId,
              tabs: await tabMetadata(conversationId, desktop, target, true),
            },
          };
        } catch (e) {
          return snapshotFailure(`snapshot parse failed: ${(e as Error).message}`);
        }
      }
      case "click": {
        if (
          typeof args.ref === "number" &&
          (typeof args.snapshot_id !== "string" || !args.snapshot_id)
        ) {
          return {
            ok: false,
            error: "snapshot_id is required with ref; call browser_snapshot again",
          };
        }
        const r = await performAction(desktop, target.viewId, "click", pageArgs);
        if (!r?.ok) return { ok: false, error: r?.error ?? "click failed" };
        const error = lookupError(r.result);
        if (error) return { ok: false, error };
        return { ok: true };
      }
      case "type": {
        if (
          typeof args.ref === "number" &&
          (typeof args.snapshot_id !== "string" || !args.snapshot_id)
        ) {
          return {
            ok: false,
            error: "snapshot_id is required with ref; call browser_snapshot again",
          };
        }
        const r = await performAction(desktop, target.viewId, "type", pageArgs);
        if (!r?.ok) return { ok: false, error: r?.error ?? "type failed" };
        const error = lookupError(r.result);
        if (error) return { ok: false, error };
        return { ok: true };
      }
      default:
        return { ok: false, error: `Unknown browser action: ${action}` };
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

/** POST the atomic claim FIRST. The server does a check-and-set: the winning
 *  renderer gets `{claimed:true, claim_token}`; losers get `{claimed:false}`
 *  and drop the action. Returns the claim token, or null when this renderer did
 *  not win (or the claim call failed — treat as "not ours"). */
async function claimAction(conversationId: string, actionId: string): Promise<string | null> {
  try {
    const resp = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/browser/action_claim/${encodeURIComponent(actionId)}`,
      { method: "POST" },
    );
    if (!resp.ok) return null;
    const body = (await resp.json()) as { claimed?: boolean; claim_token?: string };
    if (body?.claimed && typeof body.claim_token === "string") return body.claim_token;
    return null;
  } catch (e) {
    console.warn("[browser-relay] claim failed", e);
    return null;
  }
}

/** POST the action result WITH the claim token so the server can resolve the
 *  parked Future (and reject any tokenless / mismatched attempt). Best-effort:
 *  a network blip here surfaces to the agent as the server's action timeout. */
async function postResult(
  conversationId: string,
  actionId: string,
  claimToken: string,
  result: ActionResult,
): Promise<void> {
  try {
    await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/browser/action_result/${encodeURIComponent(actionId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result, claim_token: claimToken }),
      },
    );
  } catch (e) {
    // Backend blip — the server's action timeout surfaces it to the agent; log for maintainers.
    console.warn("[browser-relay] POST result failed", e);
  }
}

/**
 * Register the embedded-browser relay. No-op outside Electron. Mounted for the
 * active conversation (from `AppShell`), but it serves EVERY conversation's
 * actions: an action is claimed, dispatched, and answered against the session
 * whose stream delivered it (`sourceConversationId`), not the mounted one — a
 * background conversation can issue a browser action while another is on screen,
 * and its WebContentsView is keyed by that session. Claiming at the mounted
 * (visible) session would be rejected by the server as an owner mismatch.
 *
 * @param conversationId Gate only — the relay registers while a conversation is
 *   open. Routing uses the delivering conversation, not this.
 */
export function useBrowserAgentRelay(conversationId: string | null | undefined): void {
  const relayEnabled = !!conversationId;
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!relayEnabled) return;
    if (!supportsBrowser()) return;

    let cancelled = false;
    const handler = async (evt: BrowserActionRequestEvent, sourceConversationId: string | null) => {
      if (!sourceConversationId) return; // no delivering session — nothing to target
      const desktop = getBrowserDesktop();
      if (!desktop) return; // no browser-capable shell — nothing to claim
      // Claim FIRST — only the winner proceeds, so two windows can't double-execute.
      const claimToken = await claimAction(sourceConversationId, evt.actionId);
      if (!claimToken) return;
      if (cancelled) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "browser relay context changed",
        });
        return;
      }
      if (evt.action === "navigate" && !String(evt.args.url ?? "")) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "url is required",
        });
        return;
      }
      if (evt.action === "navigate" && !desktop.browserOpenOrNavigate) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "this desktop shell does not support the browser pane",
        });
        return;
      }
      const target = resolveBrowserTarget(sourceConversationId, evt.action, evt.args);
      if ("ok" in target) {
        await postResult(sourceConversationId, evt.actionId, claimToken, target);
        return;
      }
      let sourceHostId: string | null = null;
      if (evt.action === "navigate") {
        try {
          // Parent hints cannot establish which ancestor has the nearest own host binding.
          const visited = new Set<string>();
          let id: string | null = sourceConversationId;
          while (id !== null && !visited.has(id)) {
            visited.add(id);
            const hopId: string = id;
            // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
            const source: Session = await queryClient.fetchQuery({
              queryKey: ["session", hopId],
              queryFn: () => getSessionSlim(hopId),
              staleTime: Infinity,
              retry: false,
            });
            setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
            setSessionParent(source.id, source.parentSessionId);
            if (source.hostId) {
              sourceHostId = source.hostId;
              break;
            }
            id = source.parentSessionId;
          }
        } catch {
          // Unknown provenance stays denied for localhost; public browsing still works.
        }
      }
      if (cancelled) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "browser relay context changed",
        });
        return;
      }
      if (isBrowserTabClosing(sourceConversationId, target.tabId)) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "tab_id is closing in this session",
        });
        return;
      }
      if (
        target.kind === "open" &&
        !readBrowserTabsState(sourceConversationId).tabs.includes(target.tabId)
      ) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "tab_id is no longer open in this session",
        });
        return;
      }
      if (target.kind === "new") createBrowserTab(sourceConversationId, target.tabId);
      if (target.kind === "reserved") openAgentBrowserTab(sourceConversationId);
      if (evt.action === "screenshot" && target.kind === "legacy") {
        try {
          const existing = await desktop.browserHasView?.(target.viewId);
          if (
            !cancelled &&
            !isBrowserTabClosing(sourceConversationId, target.tabId) &&
            (!desktop.browserHasView || existing?.exists)
          )
            openAgentBrowserTab(sourceConversationId);
        } catch {
          // Capture still reports the native error if its view disappeared.
        }
      }
      if (cancelled) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "browser relay context changed",
        });
        return;
      }
      if (isBrowserTabClosing(sourceConversationId, target.tabId)) {
        await postResult(sourceConversationId, evt.actionId, claimToken, {
          ok: false,
          error: "tab_id is closing in this session",
        });
        return;
      }
      if (surfacesBrowserPane(evt.action)) {
        selectBrowserTab(sourceConversationId, target.tabId);
        emitBrowserActionClaimed(sourceConversationId, target.tabId);
      }
      const rememberedArcaHostId = evt.action === "navigate" ? readArcaHostId() : null;
      const sourceIsArcaHost =
        evt.action === "navigate" && sourceHostId !== null
          ? queryClient
              .getQueriesData<Host[]>({ queryKey: ["hosts"] })
              .flatMap(([, rows]) => rows ?? [])
              .some(
                (host) => host.host_id === sourceHostId && isArcaHost(host, rememberedArcaHostId),
              )
          : false;
      const result = await dispatch(
        sourceConversationId,
        target,
        evt.action,
        evt.args,
        desktop,
        sourceHostId,
        rememberedArcaHostId,
        sourceIsArcaHost,
      );
      await postResult(sourceConversationId, evt.actionId, claimToken, result);
    };

    const unsubscribe = onBrowserActionRequest(handler);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [relayEnabled, queryClient]);
}
