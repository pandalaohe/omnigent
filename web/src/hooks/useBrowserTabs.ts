import { useCallback, useEffect, useMemo, useState } from "react";
import { onBrowserActionClaimed } from "@/lib/browserActionBus";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/lib/browserTabs";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

export { AGENT_BROWSER_TAB_ID, browserViewId, browserViewOwnerId } from "@/lib/browserTabs";

/** Colons would make the native view owner ambiguous. */
export function validBrowserTabId(tabId: string): boolean {
  return tabId.length > 0 && !tabId.includes(":");
}

interface BrowserTabsState {
  tabs: string[];
  selected: string | null;
}

interface BrowserPage {
  url?: string;
  title?: string;
}

interface BrowserTabsBridge {
  browserHasView?: (
    viewId: string,
  ) => Promise<BrowserPage & { exists: boolean; pendingUrl?: string }>;
  onBrowserUrlChanged?: (
    callback: (page: BrowserPage & { conversationId: string }) => void,
  ) => () => void;
}

const agentNavigationEpochs = new Map<string, number>();
const closingViews = new Map<string, number>();

export function isBrowserTabClosing(conversationId: string, tabId: string): boolean {
  return (closingViews.get(browserViewId(conversationId, tabId)) ?? 0) > 0;
}

function agentNavigationEpoch(conversationId: string): number {
  return agentNavigationEpochs.get(conversationId) ?? 0;
}

export function readBrowserTabsState(conversationId: string): BrowserTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = (saved.openBrowsers ?? []).filter(validBrowserTabId);
  return {
    tabs,
    selected: tabs.includes(saved.selectedBrowserId ?? "") ? saved.selectedBrowserId! : null,
  };
}

/** Create one independent browser tab, selected in its owning session. */
export function createBrowserTab(
  conversationId: string,
  tabId: string = crypto.randomUUID(),
): string {
  const current = readBrowserTabsState(conversationId);
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: [...current.tabs, tabId],
    selectedBrowserId: tabId,
  });
  return tabId;
}

/** Select an already-open tab; the caller must validate ownership first. */
export function selectBrowserTab(conversationId: string, tabId: string): BrowserTabsState {
  const current = readBrowserTabsState(conversationId);
  if (!current.tabs.includes(tabId)) return current;
  if (tabId === AGENT_BROWSER_TAB_ID) {
    agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  }
  const next = { ...current, selected: tabId };
  writeSessionWorkspaceState(conversationId, { selectedBrowserId: tabId });
  return next;
}

/** Persist a browser view as a selected soft tab for its owning session. */
export function openBrowserTab(conversationId: string, tabId: string): BrowserTabsState {
  if (tabId === AGENT_BROWSER_TAB_ID) {
    agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  }
  const current = readBrowserTabsState(conversationId);
  const next = {
    tabs: current.tabs.includes(tabId) ? current.tabs : [...current.tabs, tabId],
    selected: tabId,
  };
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: next.tabs,
    selectedBrowserId: next.selected,
  });
  return next;
}

/** Persist the agent browser as a selected soft tab for a session. */
export function openAgentBrowserTab(conversationId: string): BrowserTabsState {
  return openBrowserTab(conversationId, AGENT_BROWSER_TAB_ID);
}

export function useBrowserTabs(conversationId: string) {
  const [state, setState] = useState(() => ({
    ownerId: conversationId,
    ...readBrowserTabsState(conversationId),
  }));
  // A new session can render before the migration effect. Never relabel the
  // outgoing session's tabs as belonging to the incoming one.
  const { tabs: visibleTabs, selected: visibleSelected } = useMemo(
    () => (state.ownerId === conversationId ? state : readBrowserTabsState(conversationId)),
    [state, conversationId],
  );
  const [pages, setPages] = useState<Record<string, BrowserPage>>({});
  const viewIdsKey = JSON.stringify(
    visibleTabs.map((tabId) => browserViewId(conversationId, tabId)),
  );
  useEffect(() => {
    setState({ ownerId: conversationId, ...readBrowserTabsState(conversationId) });
  }, [conversationId]);

  useEffect(() => {
    const viewIds = new Set<string>(JSON.parse(viewIdsKey));
    setPages((current) => {
      const entries = Object.entries(current);
      const retained = entries.filter(([viewId]) => viewIds.has(viewId));
      return retained.length === entries.length ? current : Object.fromEntries(retained);
    });
    const bridge = (window as unknown as { omnigentDesktop?: BrowserTabsBridge }).omnigentDesktop;
    if (!bridge || viewIds.size === 0) return;
    let active = true;
    const updated = new Set<string>();
    const setPage = (viewId: string, page: BrowserPage) => {
      setPages((current) => ({ ...current, [viewId]: page }));
    };
    const unsubscribe = bridge.onBrowserUrlChanged?.((page) => {
      if (!active || !viewIds.has(page.conversationId)) return;
      updated.add(page.conversationId);
      setPage(page.conversationId, { url: page.url, title: page.title });
    });
    const restore = async (viewId: string) => {
      try {
        const page = await bridge.browserHasView?.(viewId);
        // Live updates take precedence over an earlier restore snapshot.
        if (!active || updated.has(viewId) || !page) return;
        setPage(
          viewId,
          page.exists
            ? {
                url: page.url && page.url !== "about:blank" ? page.url : page.pendingUrl,
                title: page.title,
              }
            : {},
        );
      } catch {
        // A closed view or an older shell can leave the default tab label.
      }
    };
    for (const viewId of viewIds) void restore(viewId);
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [viewIdsKey]);

  const labelFor = (tabId: string, index: number): string => {
    const page = pages[browserViewId(conversationId, tabId)];
    const title = page?.title?.trim();
    if (page?.url) {
      try {
        const hostname = new URL(page.url).hostname;
        // Chromium supplies the URL as the title for untitled documents.
        const urlTitle = page.url.replace(/^https?:\/\//, "");
        if (
          title &&
          title !== page.url &&
          title !== urlTitle &&
          title !== urlTitle.replace(/\/$/, "")
        ) {
          return title;
        }
        if (hostname) return hostname;
      } catch {
        // Blank or invalid URLs keep the numbered placeholder.
      }
    }
    return title && title !== "about:blank" ? title : `Browser ${index + 1}`;
  };

  const update = useCallback(
    (mutate: (current: BrowserTabsState) => BrowserTabsState) => {
      const next = mutate(readBrowserTabsState(conversationId));
      writeSessionWorkspaceState(conversationId, {
        openBrowsers: next.tabs,
        selectedBrowserId: next.selected,
      });
      setState({ ownerId: conversationId, ...next });
    },
    [conversationId],
  );

  const select = (selected: string | null) => {
    update((current) => ({ ...current, selected }));
  };

  useEffect(() => {
    const selectBrowser = (sourceConversationId: string, tabId: string) => {
      if (sourceConversationId === conversationId) {
        setState({ ownerId: conversationId, ...openBrowserTab(conversationId, tabId) });
      }
    };
    const unsubscribeAction = onBrowserActionClaimed((sourceConversationId, tabId) => {
      if (sourceConversationId === conversationId) {
        setState({ ownerId: conversationId, ...selectBrowserTab(conversationId, tabId) });
      }
    });
    const unsubscribeLink = onInAppLinkOpen(selectBrowser);
    return () => {
      unsubscribeAction();
      unsubscribeLink();
    };
  }, [conversationId]);

  const add = () => {
    createBrowserTab(conversationId);
    setState({ ownerId: conversationId, ...readBrowserTabsState(conversationId) });
  };

  const close = async (tabId: string): Promise<boolean> => {
    const navigationEpoch = agentNavigationEpoch(conversationId);
    const viewId = browserViewId(conversationId, tabId);
    closingViews.set(viewId, (closingViews.get(viewId) ?? 0) + 1);
    try {
      const desktop = (
        window as unknown as {
          omnigentDesktop?: {
            browserClose?: (viewId: string) => Promise<{ ok: boolean }>;
          };
        }
      ).omnigentDesktop;
      const result = await desktop?.browserClose?.(viewId).catch(() => ({ ok: false }));
      if (result && !result.ok) return false;
      // A fresh agent/link navigation supersedes an older close of the reserved
      // agent view. Keep the recreated tab selected when the close resolves late.
      if (
        tabId === AGENT_BROWSER_TAB_ID &&
        agentNavigationEpoch(conversationId) !== navigationEpoch
      ) {
        return true;
      }
      update((previous) => {
        const index = previous.tabs.indexOf(tabId);
        if (index === -1) return previous;
        const tabs = previous.tabs.filter((id) => id !== tabId);
        const selected =
          previous.selected === tabId ? (tabs[Math.max(0, index - 1)] ?? null) : previous.selected;
        return { tabs, selected };
      });
      return true;
    } finally {
      const active = closingViews.get(viewId) ?? 0;
      if (active <= 1) closingViews.delete(viewId);
      else closingViews.set(viewId, active - 1);
    }
  };

  return {
    tabs: visibleTabs,
    selected: visibleSelected,
    labelFor,
    add,
    close,
    select,
    viewId: visibleSelected === null ? null : browserViewId(conversationId, visibleSelected),
    agentBrowser: visibleSelected === AGENT_BROWSER_TAB_ID,
  };
}
