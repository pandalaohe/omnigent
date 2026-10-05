import { useEffect, useRef } from "react";

import type { Conversation } from "@/hooks/useConversations";
import type { AnyBlock } from "@/lib/blocks";
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

// Seen-card counts live client-side because the server deliberately does not
// bump `updated_at` when a card arrives, so "the user saw the card" has no
// timestamp to derive from. In-memory only: a reload re-raises rows whose
// cards are still open.
const elicitationAcknowledgements = new Map<string, number>();

/**
 * Records how many of the open session's pending cards the user has seen.
 * ChatPage calls it on the same refresh cadence as useMarkConversationSeen;
 * a later count above the recorded one puts the row back in the
 * needs-response tier.
 */
export function useAcknowledgePendingElicitations(
  conversationId: string | undefined,
  pendingCount: number | undefined,
): void {
  useEffect(() => {
    if (!conversationId || pendingCount === undefined) return;
    elicitationAcknowledgements.set(conversationId, pendingCount);
  }, [conversationId, pendingCount]);
}

/**
 * How many pending cards the open session should acknowledge. `blocks` is the
 * chat store's active transcript and `blocksConversationId` names the session
 * it belongs to; a route switch leaves the previous session's blocks in the
 * store briefly, so they only count once the store names the open session.
 * The sidebar row can be unloaded (a deep link outside the loaded pages), so
 * the loaded row's count seeds the fallback; the transcript's own pending
 * cards win when larger. Child/sub-agent cards mirrored into an ancestor chat
 * carry the child's session id, so they don't count against the ancestor's
 * row.
 */
export function pendingCardsToAcknowledge(
  blocks: readonly AnyBlock[],
  conversationId: string | undefined,
  loadedPendingCount: number | undefined,
  blocksConversationId: string | null | undefined,
): number {
  const loaded = loadedPendingCount ?? 0;
  if (!conversationId || blocksConversationId !== conversationId) return loaded;
  let count = 0;
  for (const block of blocks) {
    if (block.type !== "elicitation" || block.status !== "pending") continue;
    if (block.targetSessionId && block.targetSessionId !== conversationId) continue;
    count += 1;
  }
  return Math.max(count, loaded);
}

// A count can only drop because cards were answered. Keep the acknowledged
// baseline at the lowest observed count so the NEXT card re-raises the row
// instead of hiding behind an answered-then-equal total.
function observeElicitationCounts(rows: readonly Conversation[]): void {
  for (const row of rows) {
    const stored = elicitationAcknowledgements.get(row.id);
    if (stored === undefined) continue;
    const count = row.pending_elicitations_count ?? 0;
    if (count >= stored) continue;
    if (count === 0) elicitationAcknowledgements.delete(row.id);
    else elicitationAcknowledgements.set(row.id, count);
  }
}

/** Test-only: the acknowledgement map is module state; clear it between specs. */
export function resetElicitationAcknowledgementsForTests(): void {
  elicitationAcknowledgements.clear();
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
  /** Rows with a card the user has not opened since it arrived (tier 0). */
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
 * The next Poll target: unacknowledged needs-response rows first (any row,
 * oldest first), then unread rows (windowed, oldest first; background rows
 * only when not deprioritized), then the plain circular cycle after the
 * active row, skipping collapsed and visited rows. The active row is never
 * its own target.
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
      observeElicitationCounts(allRows);
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
          needsResponse: (row) =>
            (row.pending_elicitations_count ?? 0) > (elicitationAcknowledgements.get(row.id) ?? 0),
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
        const active = allRows.find((row) => row.id === operation.activeId);
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
