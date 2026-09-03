import { useEffect, useRef } from "react";

import type { Conversation } from "@/hooks/useConversations";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { getConversationForegroundStatus } from "@/hooks/useSessionState";
import { isConversationUnseen, seedReadState } from "@/hooks/useUnseenConversations";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";
import { useNavigate } from "@/lib/routing";
import { isSessionInsidePollingWindow } from "@/lib/sessionNavigationPreferences";

export const POLL_SESSIONS_ACTION_EVENT = "omnigent:action:poll-sessions";
export const ARCHIVE_SESSION_ACTION_EVENT = "omnigent:action:archive-session";

export function dispatchPollSessions(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT));
}

export function dispatchArchiveSession(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT));
}

function pollingPriority(
  conversation: Conversation,
  unread: boolean,
  deprioritizeBackgroundSessions: boolean,
): number {
  const actionable = (conversation.pending_elicitations_count ?? 0) > 0;
  if (!deprioritizeBackgroundSessions) {
    if (actionable) return 0;
    return unread ? 1 : 2;
  }

  const background = (conversation.background_activity_count ?? 0) > 0;
  if (!background) {
    if (actionable) return 0;
    return unread ? 1 : 4;
  }
  if (actionable) return 2;
  return unread ? 3 : 5;
}

/** Select action/result-first in circular order, optionally keeping B rows secondary. */
export function choosePolledConversation(
  conversations: readonly Conversation[],
  activeId: string | undefined,
  isUnread: (conversation: Conversation) => boolean,
  deprioritizeBackgroundSessions = false,
): Conversation | null {
  if (conversations.length === 0) return null;
  const activeIndex = activeId ? conversations.findIndex((row) => row.id === activeId) : -1;
  const start = activeIndex >= 0 ? activeIndex + 1 : 0;
  let selected: Conversation | null = null;
  let selectedPriority = Number.POSITIVE_INFINITY;
  for (let offset = 0; offset < conversations.length; offset++) {
    const candidate = conversations[(start + offset) % conversations.length];
    if (!candidate || candidate.id === activeId) continue;
    const unread = isUnread(candidate);
    const priority = pollingPriority(candidate, unread, deprioritizeBackgroundSessions);
    if (priority < selectedPriority) {
      selected = candidate;
      selectedPriority = priority;
    }
  }
  return selected;
}

export interface SessionPollingHotkeysOptions {
  activeId: string | undefined;
  getConversations: () => Promise<Conversation[]>;
  onArchive: (id: string) => Promise<unknown>;
  isUnread?: (conversation: Conversation) => boolean;
  canArchive?: (conversation: Conversation) => boolean;
}

export function useSessionPollingHotkeys(options: SessionPollingHotkeysOptions): void {
  const navigate = useNavigate();
  const { pollingActiveWindowHours, deprioritizeBackgroundSessions } =
    useSessionNavigationPreferences();
  const latest = useRef({
    ...options,
    pollingActiveWindowHours,
    deprioritizeBackgroundSessions,
  });
  latest.current = { ...options, pollingActiveWindowHours, deprioritizeBackgroundSessions };
  const busy = useRef(false);

  useEffect(() => {
    const loadTarget = async (operation: typeof latest.current) => {
      const allRows = (await operation.getConversations()).filter((row) => row.archived !== true);
      // The sidebar normally seeds this mirror after React commits its freshly
      // loaded pages. Polling can run in that gap, so seed synchronously from
      // the complete list before applying unread-first selection.
      seedReadState(allRows);
      const unread = (conversation: Conversation) =>
        operation.isUnread?.(conversation) ??
        isConversationUnseen(
          conversation.id,
          conversation.updated_at,
          getConversationForegroundStatus(conversation),
        );
      const eligibleRows = allRows.filter((row) =>
        isSessionInsidePollingWindow(row.updated_at, operation.pollingActiveWindowHours),
      );
      return {
        allRows,
        target: choosePolledConversation(
          eligibleRows,
          operation.activeId,
          unread,
          operation.deprioritizeBackgroundSessions,
        ),
      };
    };

    const poll = async () => {
      if (busy.current) return;
      busy.current = true;
      const operation = latest.current;
      try {
        const { target } = await loadTarget(operation);
        // A list request may resolve after the user already chose another
        // session. Never let that stale operation pull the route backwards.
        if (target && latest.current.activeId === operation.activeId) {
          navigate(`/c/${target.id}`);
        }
      } finally {
        busy.current = false;
      }
    };

    const archive = async () => {
      if (busy.current || !latest.current.activeId) return;
      busy.current = true;
      const operation = latest.current;
      try {
        const { allRows, target } = await loadTarget(operation);
        // The window narrows only the next-target candidates. An older active
        // session must remain archivable, otherwise enabling the filter would
        // silently disable the archive hotkey on that session.
        const active = allRows.find((row) => row.id === operation.activeId);
        if (!active || operation.canArchive?.(active) === false) return;
        await operation.onArchive(active.id);
        if (latest.current.activeId === operation.activeId) {
          navigate(target ? `/c/${target.id}` : "/", { replace: true });
        }
      } finally {
        busy.current = false;
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing || event.defaultPrevented) return;
      if (eventMatchesShortcutAction(event, "pollSessions")) {
        event.preventDefault();
        event.stopPropagation();
        void poll();
      } else if (eventMatchesShortcutAction(event, "archiveSession")) {
        event.preventDefault();
        event.stopPropagation();
        void archive();
      }
    };
    const onPoll = () => void poll();
    const onArchive = () => void archive();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener(POLL_SESSIONS_ACTION_EVENT, onPoll);
    window.addEventListener(ARCHIVE_SESSION_ACTION_EVENT, onArchive);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener(POLL_SESSIONS_ACTION_EVENT, onPoll);
      window.removeEventListener(ARCHIVE_SESSION_ACTION_EVENT, onArchive);
    };
  }, [navigate]);
}
