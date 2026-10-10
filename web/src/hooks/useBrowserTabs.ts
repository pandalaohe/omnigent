import { useCallback, useEffect, useMemo, useState } from "react";
import { onBrowserActionClaimed } from "@/lib/browserActionBus";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

/** Stable soft-tab id for the browser view driven by agents and opted-in links. */
export const AGENT_BROWSER_TAB_ID = "agent-browser";

/** A colon would make the derived native view ID ambiguous to its owner parser. */
export function validBrowserTabId(tabId: string): boolean {
  return tabId.length > 0 && !tabId.includes(":");
}

export function browserViewId(conversationId: string, tabId: string): string {
  return tabId === AGENT_BROWSER_TAB_ID
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
}

/** Owning session ID for a browser view ID; mirrors the Electron view-registry parser. */
export function browserViewOwnerId(viewId: string): string {
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (!tab) return viewId;
  try {
    return decodeURIComponent(tab[1]);
  } catch {
    return viewId;
  }
}

interface BrowserTabsState {
  tabs: string[];
  selected: string | null;
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

function withAgentBrowserSelected(current: BrowserTabsState): BrowserTabsState {
  return {
    tabs: current.tabs.includes(AGENT_BROWSER_TAB_ID)
      ? current.tabs
      : [...current.tabs, AGENT_BROWSER_TAB_ID],
    selected: AGENT_BROWSER_TAB_ID,
  };
}

/** Persist the agent/link browser as a selected soft tab for a session. */
export function openAgentBrowserTab(conversationId: string): BrowserTabsState {
  agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  const next = withAgentBrowserSelected(readBrowserTabsState(conversationId));
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: next.tabs,
    selectedBrowserId: next.selected,
  });
  return next;
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
  useEffect(() => {
    setState({ ownerId: conversationId, ...readBrowserTabsState(conversationId) });
  }, [conversationId]);

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
    const selectAgentBrowser = (sourceConversationId: string) => {
      if (sourceConversationId === conversationId) {
        setState({ ownerId: conversationId, ...openAgentBrowserTab(conversationId) });
      }
    };
    const unsubscribeAction = onBrowserActionClaimed((sourceConversationId, tabId) => {
      if (sourceConversationId === conversationId) {
        setState({ ownerId: conversationId, ...selectBrowserTab(conversationId, tabId) });
      }
    });
    const unsubscribeLink = onInAppLinkOpen(selectAgentBrowser);
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
    add,
    close,
    select,
    viewId: visibleSelected === null ? null : browserViewId(conversationId, visibleSelected),
    agentBrowser: visibleSelected === AGENT_BROWSER_TAB_ID,
  };
}
