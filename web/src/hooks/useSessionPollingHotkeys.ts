import { useEffect, useRef } from "react";
import { toast } from "sonner";

import type { Conversation } from "@/hooks/useConversations";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { getConversationForegroundStatus } from "@/hooks/useSessionState";
import { isConversationUnseen, seedReadState } from "@/hooks/useUnseenConversations";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";
import { useNavigate } from "@/lib/routing";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { getSessionSlim } from "@/lib/sessionsApi";
import { isSessionInsidePollingWindow } from "@/lib/sessionNavigationPreferences";

export const POLL_SESSIONS_ACTION_EVENT = "omnigent:action:poll-sessions";
export const ARCHIVE_SESSION_ACTION_EVENT = "omnigent:action:archive-session";

export function dispatchPollSessions(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT));
}

export function dispatchArchiveSession(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT));
}

/** B/G rows: background activity or an active/paused goal. A foreground spinner is neither. */
function isBackgroundSession(conversation: Conversation): boolean {
  return (
    (conversation.background_activity_count ?? 0) > 0 ||
    conversation.goal_state === "active" ||
    conversation.goal_state === "paused"
  );
}

/** First row by updated_at; ties keep sidebar order. Rows must be non-empty. */
function oldestFirst(rows: readonly Conversation[]): Conversation {
  let oldest = rows[0] as Conversation;
  for (const row of rows) {
    if (row.updated_at < oldest.updated_at) oldest = row;
  }
  return oldest;
}

export interface PollingChoice {
  isUnread: (conversation: Conversation) => boolean;
  /** Rows with a pending card (tier 0). */
  needsResponse?: (conversation: Conversation) => boolean;
  /** Rows hidden inside a collapsed section/folder: the plain cycle skips them. */
  isCollapsed?: (conversation: Conversation) => boolean;
  /** The optional active-hours window; needs-response rows ignore it. */
  isInsideWindow?: (conversation: Conversation) => boolean;
  /** On (the default): background rows are ineligible for the unread tier. */
  deprioritizeBackgroundSessions?: boolean;
  /** Rows already visited this round; only the plain cycle consults it. */
  visited?: ReadonlySet<string>;
}

/**
 * The next Poll target: rows with a pending card first (any row, oldest
 * first), then unread rows (windowed, oldest first; background rows only
 * when not deprioritized), then the plain circular cycle after the active
 * row, skipping collapsed and visited rows. The active row is never its own
 * target.
 */
export function choosePolledConversation(
  conversations: readonly Conversation[],
  activeId: string | undefined,
  choice: PollingChoice,
): Conversation | null {
  const others = conversations.filter((row) => row.id !== activeId);
  if (others.length === 0) return null;
  const needsResponse = choice.needsResponse ?? (() => false);
  const isCollapsed = choice.isCollapsed ?? (() => false);
  const isInsideWindow = choice.isInsideWindow ?? (() => true);
  const visited = choice.visited ?? new Set<string>();

  const cards = others.filter(needsResponse);
  if (cards.length > 0) return oldestFirst(cards);

  const unread = others.filter(
    (row) =>
      isInsideWindow(row) &&
      choice.isUnread(row) &&
      (!choice.deprioritizeBackgroundSessions || !isBackgroundSession(row)),
  );
  if (unread.length > 0) return oldestFirst(unread);

  const cyclable = new Set(
    others
      .filter((row) => isInsideWindow(row) && !isCollapsed(row) && !visited.has(row.id))
      .map((row) => row.id),
  );
  const activeIndex = conversations.findIndex((row) => row.id === activeId);
  const start = activeIndex >= 0 ? activeIndex + 1 : 0;
  for (let offset = 0; offset < conversations.length; offset++) {
    const candidate = conversations[(start + offset) % conversations.length];
    if (candidate && cyclable.has(candidate.id)) return candidate;
  }
  return null;
}

export interface SessionPollingHotkeysOptions {
  activeId: string | undefined;
  getConversations: () => Promise<Conversation[]>;
  // The whole row, not just its id: the post-archive Undo pill re-injects the
  // archived rows into the sidebar, so the caller needs them in hand.
  onArchive: (conversation: Conversation) => Promise<unknown>;
  isUnread?: (conversation: Conversation) => boolean;
  /** Rows hidden inside a collapsed section/folder: the plain cycle skips them. */
  isCollapsed?: (conversation: Conversation) => boolean;
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
  const pollCycle = useRef<{
    /** Route a Poll action most recently selected; a different route means manual navigation. */
    expectedActiveId: string | undefined;
    /** Rows already visited this round, including the round's starting session. */
    visited: Set<string>;
  }>({ expectedActiveId: undefined, visited: new Set() });

  // A route change the user made by hand (Poll did not select it) starts a
  // fresh round the moment it lands — even if the user later navigates back
  // to the row the last Poll selected. Keep the current session in the visited
  // set so the first target is always another row.
  useEffect(() => {
    const cycle = pollCycle.current;
    if (cycle.expectedActiveId === options.activeId) return;
    cycle.expectedActiveId = options.activeId;
    cycle.visited = new Set(options.activeId ? [options.activeId] : []);
  }, [options.activeId]);

  useEffect(() => {
    const loadRows = async (operation: typeof latest.current) => {
      const allRows = (await operation.getConversations()).filter((row) => row.archived !== true);
      // The sidebar normally seeds this mirror after React commits its freshly
      // loaded pages. Polling can run in that gap, so seed synchronously from
      // the complete list before reading the unread tier.
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
      const eligibleIds = new Set(eligibleRows.map((row) => row.id));
      const choose = (visited?: ReadonlySet<string>) =>
        choosePolledConversation(allRows, operation.activeId, {
          isUnread: unread,
          needsResponse: (row) => (row.pending_elicitations_count ?? 0) > 0,
          isCollapsed: (row) => operation.isCollapsed?.(row) ?? false,
          isInsideWindow: (row) => eligibleIds.has(row.id),
          deprioritizeBackgroundSessions: operation.deprioritizeBackgroundSessions,
          visited,
        });
      return { allRows, eligibleRows, choose };
    };

    const poll = async () => {
      if (busy.current) return;
      busy.current = true;
      const operation = latest.current;
      try {
        const { eligibleRows, choose } = await loadRows(operation);
        const eligibleIds = new Set(eligibleRows.map((row) => row.id));
        const cycle = pollCycle.current;

        // Drop ids that left the eligible window since the previous press, and
        // keep the current session visited so it is never its own target. The
        // manual-route reset above owns starting a new round.
        cycle.visited = new Set([...cycle.visited].filter((id) => eligibleIds.has(id)));
        if (operation.activeId) cycle.visited.add(operation.activeId);

        // Every cyclable row had a visit: begin the next round from the active
        // row. Collapsed rows are not cyclable, so they can't hold a round open.
        const cyclable = eligibleRows.filter(
          (row) => row.id !== operation.activeId && !(operation.isCollapsed?.(row) ?? false),
        );
        if (cyclable.length > 0 && cyclable.every((row) => cycle.visited.has(row.id))) {
          cycle.visited = new Set(operation.activeId ? [operation.activeId] : []);
        }

        const target = choose(cycle.visited);
        // A list request may resolve after the user already chose another
        // session. Never let that stale operation pull the route backwards.
        if (target && latest.current.activeId === operation.activeId) {
          cycle.visited.add(target.id);
          cycle.expectedActiveId = target.id;
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
        const { allRows, choose } = await loadRows(operation);
        const target = choose();
        // The window narrows only the next-target candidates. An older active
        // session must remain archivable, otherwise enabling the filter would
        // silently disable the archive hotkey on that session.
        let active = allRows.find((row) => row.id === operation.activeId);
        // Children are absent from the sidebar population. Resolve the selected
        // session itself rather than archiving its top-level ancestor.
        if (!active && operation.activeId) {
          try {
            const session = await getSessionSlim(operation.activeId);
            if (
              session.id !== operation.activeId ||
              session.archived ||
              !isOwnerLevel(session.permissionLevel)
            ) {
              return;
            }
            active = {
              id: session.id,
              object: "conversation",
              title: session.title,
              created_at: session.createdAt,
              updated_at: session.updatedAt ?? session.createdAt,
              labels: session.labels ?? {},
              permission_level: session.permissionLevel,
              archived: false,
              parent_session_id: session.parentSessionId,
            };
          } catch {
            toast.error("Couldn't load the session to archive");
            return;
          }
        }
        if (!active || operation.canArchive?.(active) === false) return;
        await operation.onArchive(active);
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
