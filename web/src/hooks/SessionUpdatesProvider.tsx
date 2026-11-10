import { SidebarDataContext } from "./useSidebarData";
import {
  type Conversation,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
} from "./useConversations";
// App-level wiring for the `WS /v1/sessions/updates` push stream.
//
// Mounted once near the app root. It:
//   1. opens the shared session-updates WebSocket for the tab's lifetime,
//   2. derives the watch-set from whatever conversation ids are currently
//      cached across the sidebar's `["conversations", ...]` query variants,
//      and pushes it to the socket, and
//   3. applies incoming snapshot/changed/removed frames back into that cache
//      — patching display fields in place and reconciling membership changes
//      promptly; changed timestamps trigger a throttled list refresh so the
//      cached rows retain the server's fetched pagination order.
//
// This replaces the old 4 s list poll; `useConversations` keeps low-rate
// HTTP reconciliation so new sessions from other tabs / CLIs are still
// discovered while the socket handles watched-row freshness.

import { type ReactNode, useCallback, useContext, useEffect, useRef } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { getCurrentUserId } from "@/lib/identity";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  isSessionArchiving,
  isSessionDeleting,
  markRecentlyCreated,
} from "@/hooks/useConversations";
import {
  type ConversationsInfiniteData,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  filtersFromProjectQueryKey,
  insertNewRowsIntoPages,
  mergeItemsIntoPages,
  nullsToUndefined,
  PROJECT_LABEL_KEY,
  removeIdsFromPages,
  settleOpenSessionRow,
  unmarkRecentlyCreated,
} from "@/lib/sessionListCache";
import { createListReconcileScheduler } from "@/lib/listReconcileScheduler";
import { isModalHostResolved, resolveModalHost } from "@/lib/sessionHost";
import { sessionVisibility } from "@/lib/sessionVisibility";
import { type SessionUpdatesFrame, sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { isTempConvId } from "@/lib/tempConversationId";

// Coalesce bursts of watch-set recomputes into one push. 250 ms is short
// enough to feel live, long enough to batch the flurry of cache writes a
// single frame can trigger.
const DEBOUNCE_MS = 250;
const ORDER_REFRESH_MS = 15_000;

/**
 * Overlay wire items onto every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param items - Wire items from a snapshot/changed frame.
 * @returns Ids not found in any cached page, whether a prompt refetch is
 *   needed, and whether fetched page order is stale.
 */
function applyItemsToCache(
  queryClient: QueryClient,
  items: SessionListWireItem[],
  viewerId?: string | null,
  activeId?: string,
): { missingIds: string[]; needsRefetch: boolean; orderStale: boolean } {
  // Frames are full rows with explicit nulls; convert null → undefined so a
  // cleared field overlays the cache in the same shape GET /v1/sessions
  // produces (absent), without tripping the permission_level === null sentinel.
  const itemsById = new Map<string, SessionListWireItem>();
  for (const item of items) {
    if (isSessionDeleting(item.id)) continue;
    itemsById.set(
      item.id,
      nullsToUndefined(isSessionArchiving(item.id) ? { ...item, archived: true } : item),
    );
  }
  // The open session is always in the watch-set, so its row arrives with every
  // snapshot whether or not the server lists it. Another user's open session
  // (e.g. opened by a link shared only through a group) is never inserted: list
  // membership is the server's call, re-read on an access_changed frame or when
  // the session's permission changes.
  const activeItem = activeId === undefined ? undefined : itemsById.get(activeId);
  const skipInsert = (id: string) =>
    isSessionDeleting(id) ||
    (id === activeId &&
      activeItem !== undefined &&
      sessionVisibility(
        { owner: activeItem.owner, permission_level: activeItem.permission_level ?? null },
        viewerId ?? null,
      ) === "shared");
  const foundAnywhere = new Set<string>();
  let needsRefetch = false;
  let orderStale = false;
  const entries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  });
  // Only search-scoped lists depend on the server's title/content matching and
  // result order, so a title/updated_at change forces the debounced refetch
  // only while one is cached (sidebar search or the command palette, plus
  // their gc window).
  const searchListCached = entries.some(
    ([key]) => filtersFromConversationQueryKey(key).searchQuery !== "",
  );
  for (const [key, data] of entries) {
    if (key[4] === "archived") continue;
    const filters = filtersFromConversationQueryKey(key);
    const {
      data: merged,
      found,
      needsRefetch: queryNeedsRefetch,
      orderStale: queryOrderStale,
    } = mergeItemsIntoPages(data, itemsById, filters, searchListCached);
    for (const id of found) foundAnywhere.add(id);
    if (queryNeedsRefetch) needsRefetch = true;
    if (queryOrderStale) orderStale = true;
    // Surface a brand-new watched session (a create here or elsewhere, a share)
    // at the top now, instead of after the debounced refetch (which lags the
    // search index). An unfiled row is fully placed → mark it found so it skips
    // that refetch; a filed row can't be placed in its folder locally → leave it
    // missing so the folder still reconciles via the scheduled refetch.
    const missingHere = new Map([...itemsById].filter(([id]) => !found.has(id)));
    const { data: next, inserted } = insertNewRowsIntoPages(
      merged,
      missingHere,
      filters,
      skipInsert,
      viewerId,
    );
    for (const row of inserted) {
      if (row.project_id == null && !row.labels?.[PROJECT_LABEL_KEY]) foundAnywhere.add(row.id);
      // Keep the new row in the list-fetch until the search index catches up,
      // so the create-path refetch (or any reconcile) can't drop it before it's
      // queryable — otherwise it flashes in and out. Mirrors the delete tombstone.
      markRecentlyCreated(row);
    }
    if (next !== data) queryClient.setQueryData(key, next);
  }
  // Each project folder fetches its own ["project-sessions", <name>] list, so
  // streamed field updates (pending_elicitations_count → "Needs response",
  // status, runner_online, …) must overlay those caches too — otherwise a
  // filed session's row stays frozen at fetch time. Each unsearched folder
  // cache retains its own live or archived slice.
  const projectEntries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["project-sessions"],
  });
  for (const [key, data] of projectEntries) {
    const {
      data: next,
      found,
      needsRefetch: queryNeedsRefetch,
      orderStale: queryOrderStale,
    } = mergeItemsIntoPages(data, itemsById, filtersFromProjectQueryKey(key), searchListCached);
    for (const id of found) foundAnywhere.add(id);
    if (queryNeedsRefetch) needsRefetch = true;
    if (queryOrderStale) orderStale = true;
    if (next !== data) queryClient.setQueryData(key, next);
  }
  queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (previous) => {
    if (!previous) return previous;
    let changed = false;
    const conversations = previous.conversations
      .map((row) => {
        const item = itemsById.get(row.id);
        if (!item) return row;
        foundAnywhere.add(row.id);
        changed = true;
        // Pin membership and pin timestamps reconcile through the pinned query.
        return {
          ...row,
          ...item,
          labels: {
            ...(item.labels ?? row.labels),
            ["omnigent.pinned"]: row.labels["omnigent.pinned"],
          },
        };
      })
      .filter((row) => !row.archived);
    return changed ? { ...previous, conversations } : previous;
  });
  // Recent keeps interaction order, independently of the paginated lists.
  queryClient.setQueriesData<Conversation[]>({ queryKey: ["recent-sessions"] }, (previous) =>
    previous?.flatMap((row) => {
      const item = itemsById.get(row.id);
      if (!item) return [row];
      foundAnywhere.add(row.id);
      if (item.archived || item.parent_session_id) {
        needsRefetch = true;
        return [];
      }
      return [{ ...row, ...item }];
    }),
  );
  // The open session's fetched sidebar row sits outside every list, so a remote
  // change (archive, rename, an approval) reaches it only here.
  for (const item of itemsById.values()) settleOpenSessionRow(queryClient, item.id, item);
  return {
    // Sub-agent children are watched for their tree views, but no session list
    // ever holds them, so their absence is not a sign of a new session.
    missingIds: [...itemsById.values()]
      .filter((item) => item.parent_session_id == null && !foundAnywhere.has(item.id))
      .map((item) => item.id),
    needsRefetch,
    orderStale,
  };
}

/**
 * Ids carried by the sub-agent tree caches (`["conversation", <parent>,
 * "child_sessions"]`). Those ids never appear in a conversations page — the
 * list filters `parent_session_id` rows out — so a missing-id refetch for them
 * would reconcile nothing while firing the whole list. They are watched to get
 * streamed status; their own frames refresh the parent's child-sessions query.
 */
function collectChildSessionIds(queryClient: QueryClient): Set<string> {
  const ids = new Set<string>();
  const childEntries = queryClient.getQueriesData<unknown>({
    queryKey: ["conversation"],
  });
  for (const [key, data] of childEntries) {
    // Only ["conversation", <id>, "child_sessions", ...] entries carry child
    // lists: the active list is an array, and the past zone's infinite query
    // nests rows under `pages[].data`.
    if (!Array.isArray(key) || key[2] !== "child_sessions") continue;
    const rows = childRowsFromQueryData(data);
    if (rows === null) continue;
    for (const child of rows) ids.add(child.id);
  }
  return ids;
}

function childRowsFromQueryData(data: unknown): ChildSessionInfo[] | null {
  if (Array.isArray(data)) return data as ChildSessionInfo[];
  if (!data || typeof data !== "object") return null;
  const pages = (data as { pages?: unknown }).pages;
  if (!Array.isArray(pages)) return null;
  const rows: ChildSessionInfo[] = [];
  for (const page of pages) {
    const pageData =
      page && typeof page === "object" ? (page as { data?: unknown }).data : undefined;
    if (Array.isArray(pageData)) rows.push(...(pageData as ChildSessionInfo[]));
  }
  return rows;
}

/**
 * Remove ids from every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids to evict.
 * @returns `true` when at least one cached page changed.
 */
function removeIdsFromCache(queryClient: QueryClient, ids: string[]): boolean {
  const idSet = new Set(ids);
  let removedAny = false;
  // Both the global lists and each project folder's own list (same page shape).
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey });
    for (const [key, data] of entries) {
      const { data: next, removed } = removeIdsFromPages(data, idSet);
      if (removed) {
        queryClient.setQueryData(key, next);
        removedAny = true;
      }
    }
  }
  queryClient.setQueriesData<Conversation[]>({ queryKey: ["recent-sessions"] }, (previous) => {
    if (!previous) return previous;
    const next = previous.filter((row) => !idSet.has(row.id));
    if (next.length === previous.length) return previous;
    removedAny = true;
    return next;
  });
  return removedAny;
}

/**
 * Open the session-updates stream and keep the conversations cache in sync
 * with it for as long as the app is mounted.
 *
 * @param children - The app subtree.
 */
export function SessionUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const sidebarData = useContext(SidebarDataContext);
  const identityReady = sidebarData?.identityReady ?? true;
  const sidebarIds = sidebarData?.watchedIds;
  const sidebarIdsRef = useRef(sidebarIds);
  sidebarIdsRef.current = sidebarIds;

  // Last comments fingerprint (`count:updated_at`) seen per session id.
  // A frame whose fingerprint differs from the recorded one means a comment
  // was added, edited, addressed, or deleted somewhere else (another user,
  // the agent's update_comment tool), so the cached comment list is stale.
  // First sight of a session also invalidates: a mutation landing between
  // the comments query's fetch and the socket's first frame would otherwise
  // be swallowed as "baseline" and never surface (a real race on page load
  // when the WS connects slowly). With no comments query mounted the
  // invalidation is a no-op, so the extra firing is free. The map survives
  // socket reconnects, so changes missed while disconnected are caught by
  // the post-reconnect snapshot.
  const commentsFingerprintsRef = useRef(new Map<string, string>());

  // Last permission level streamed for the open session, so a change to it
  // (a share upgraded or revoked) reconciles list membership.
  const activePermissionRef = useRef<{ id: string; level: number | null } | null>(null);

  // The frame handler is installed once (effect deps are [queryClient]) but
  // the active route changes over the socket's lifetime. Keep the current
  // active id in a ref so the handler always reads the latest without
  // re-subscribing the socket on every navigation.
  const activeId = useActiveConversationId();
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;

  // Recompute the watch-set from the cache and push it to the socket.
  // setWatched no-ops when the id-set is unchanged, so in-place field
  // patches (and navigation between already-watched sessions) don't bounce
  // a new watch-set back to the server.
  const pushWatched = useCallback(() => {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["conversations"],
    });
    // Project folders fetch their members into their own caches; include those
    // ids so the server streams liveness (e.g. pending-elicitation "Needs
    // response") for filed sessions that aren't in the global loaded window.
    const projectEntries = queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["project-sessions"],
    });
    const ids = sidebarIdsRef.current
      ? [...sidebarIdsRef.current]
      : collectConversationIds([...entries, ...projectEntries].map(([, data]) => data));
    for (const [, rows] of queryClient.getQueriesData<Conversation[]>({
      queryKey: ["recent-sessions"],
    })) {
      for (const row of rows ?? []) {
        if (!ids.includes(row.id)) ids.push(row.id);
      }
    }
    // Union in the open session. A directly-opened child / sub-agent
    // session is filtered out of the sidebar list, so it's absent from
    // every cached conversations page and wouldn't otherwise be watched —
    // leaving the open-session view with no streamed liveness for it.
    // Adding it here makes the stream push runner_online / host_online for
    // the open session even when it's off-sidebar.
    const active = activeIdRef.current;
    if (active && !ids.includes(active)) ids.push(active);
    // Include all child session ids from the sub-agent tree caches so the
    // server streams status changes for them. When a child's changed frame
    // arrives below, we invalidate its parent's child_sessions query key,
    // giving the tree views push-style freshness without polling.
    for (const id of collectChildSessionIds(queryClient)) {
      if (!ids.includes(id)) ids.push(id);
    }
    sessionUpdatesSocket.setWatched(ids.filter((id) => !isTempConvId(id)));
  }, [queryClient]);

  // Navigating to an off-sidebar child changes the open session without
  // touching the conversations cache, so the cache subscription below
  // won't fire. Re-push on activeId change so the open session joins the
  // watch-set (and an already-watched id no-ops via setWatched).
  useEffect(() => {
    pushWatched();
  }, [pushWatched, activeId, sidebarIds]);

  // Freeze the fallback slice key (the modal host over all seen sessions) once
  // the session list first loads, then start the updates socket — which keys
  // its `?omnigent_slice_key=` off that frozen value. Gating start() on the
  // latch (rather than starting eagerly) matters ONLY for this WS: it's a
  // persistent connection, so starting pre-latch (empty map → no key) and then
  // re-keying would force a reconnect. Ordinary `authenticatedFetch` fallback
  // routes (e.g. /v1/hosts) don't gate — they just read the frozen value and a
  // pre-latch miss self-corrects on their next refetch.
  //
  // Latch on the first SUCCESSFUL conversations query, not merely a settled one:
  // the queryFn seeds `_sessionHosts` from every row before it resolves, so a
  // success means the map already reflects this user's hosts (an empty map is
  // then a genuinely zero-session user → null fallback, gate still releases). An
  // error settle is NOT latched — freezing a null key there would pin the page
  // even after a retry loads real hosts; react-query retries the list, and the
  // socket starts on that first success (nothing to stream before the list loads
  // anyway). The session list itself is NOT gated on the modal (it populates the
  // map the modal reads; gating it on the modal would deadlock).
  useEffect(() => {
    if (!identityReady) return;
    let cacheUnsub: (() => void) | null = null;
    const tryResolveAndStart = (): boolean => {
      const succeeded = queryClient
        .getQueryCache()
        .getAll()
        .some(
          (q) =>
            Array.isArray(q.queryKey) &&
            q.queryKey[0] === "conversations" &&
            q.state.status === "success",
        );
      if (!succeeded) return false;
      resolveModalHost();
      sessionUpdatesSocket.start();
      return true;
    };
    // Already succeeded (cache warm from a prior mount)? Start immediately.
    // Otherwise wait for the first success, then start once and unsubscribe.
    if (!tryResolveAndStart()) {
      cacheUnsub = queryClient.getQueryCache().subscribe(() => {
        if (isModalHostResolved() || tryResolveAndStart()) cacheUnsub?.();
      });
    }

    // Reconcile the session lists against the server when a frame shows a
    // change no local patch can place. Paced so a busy stream can't stack
    // refetch rounds against a slow server.
    const reconciler = createListReconcileScheduler(() =>
      Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["conversations"],
          predicate: (query) => query.meta?.snapshot !== true,
        }),
        // Converge each project folder's own list too (new/archived/relabeled
        // members the local field-patch can't place).
        queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
        queryClient.invalidateQueries({ queryKey: ["recent-sessions"] }),
        // Another client archiving/relabeling/deleting can add or remove a
        // project from the Archived view's picker; only local mutations
        // invalidate this scan otherwise.
        queryClient.invalidateQueries({ queryKey: ["archived-project-names"] }),
      ]),
    );
    const scheduleInvalidate = () => reconciler.schedule();
    let orderRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleOrderRefresh = () => {
      if (orderRefreshTimer !== null) return;
      orderRefreshTimer = setTimeout(() => {
        orderRefreshTimer = null;
        reconciler.schedule();
      }, ORDER_REFRESH_MS);
    };

    // See commentsFingerprintsRef: invalidate `["comments", id]` (prefix —
    // covers the per-file variants too) when a session's fingerprint moves.
    const syncCommentsFingerprints = (items: SessionListWireItem[]) => {
      for (const item of items) {
        // `?? 0` folds the wire `null` (no comments) and an older server's
        // absent field into the same baseline shape.
        const fingerprint = `${item.comments_count ?? 0}:${item.comments_updated_at ?? 0}`;
        const previous = commentsFingerprintsRef.current.get(item.id);
        commentsFingerprintsRef.current.set(item.id, fingerprint);
        if (previous !== fingerprint) {
          void queryClient.invalidateQueries({ queryKey: ["comments", item.id] });
        }
      }
    };

    let projectsDirty = false;
    const refreshProjects = () => {
      if (!projectsDirty || queryClient.isMutating({ mutationKey: ["project-order"] }) > 0) return;
      projectsDirty = false;
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-order"] });
    };
    // Replay changes after all saves, including their settlement refetches, finish.
    const unsubscribeMutations = queryClient.getMutationCache().subscribe(refreshProjects);

    const unsubscribeFrames = sessionUpdatesSocket.subscribe((frame: SessionUpdatesFrame) => {
      switch (frame.type) {
        case "heartbeat":
          return;
        case "sound_alert":
          // The sound-alert hook subscribes to the socket on its own; nothing
          // in the session-list cache reacts to a delivered alert.
          return;
        case "hosts_changed":
          void queryClient.invalidateQueries({ queryKey: ["hosts"] });
          void queryClient.invalidateQueries({ queryKey: ["session-agent"] });
          return;
        case "projects_changed":
          // Another client created/renamed/deleted a project (or changed its
          // config/icon). Only the mutating client invalidates locally, so
          // refresh the project-row caches here to converge without a reload.
          projectsDirty = true;
          refreshProjects();
          void queryClient.invalidateQueries({ queryKey: ["project-config"] });
          return;
        case "system_status_changed":
          // Payload-free nudge: the server only says "findings changed".
          // Summary consumers refetch their own permission-filtered view.
          void queryClient.invalidateQueries({ queryKey: ["system-status", "summary"] });
          return;
        case "access_changed":
          // The server decides whether the session now belongs in Shared.
          scheduleInvalidate();
          return;
        case "removed":
          for (const id of frame.ids) {
            commentsFingerprintsRef.current.delete(id);
            // A revoked share or a deletion must not be re-injected by the
            // keep-alive once the refetch correctly leaves it out.
            unmarkRecentlyCreated(id);
          }
          queryClient.setQueryData<PinnedConversationsResult>(
            PINNED_CONVERSATIONS_KEY,
            (previous) =>
              previous
                ? {
                    ...previous,
                    conversations: previous.conversations.filter(
                      (row) => !frame.ids.includes(row.id),
                    ),
                  }
                : previous,
          );
          // The open session's fetched sidebar row sits outside every list. Cancel
          // its request first so a late response can't bring the row back.
          for (const id of frame.ids) {
            const backfillKey = ["conversation-backfill", id];
            if (queryClient.getQueryState(backfillKey) === undefined) continue;
            void queryClient.cancelQueries({ queryKey: backfillKey, exact: true });
            queryClient.setQueryData<Conversation | null>(backfillKey, null);
          }
          if (removeIdsFromCache(queryClient, frame.ids)) scheduleInvalidate();
          return;
        case "snapshot":
        case "changed": {
          syncCommentsFingerprints(frame.items);
          if (frame.type === "snapshot") {
            // A snapshot restates the full watch-set, so fingerprints for
            // sessions outside it are de-watched leftovers — prune them to
            // keep the map bounded. A pruned session that re-enters the
            // watch-set re-baselines via the first-sight invalidation.
            const watchedIds = new Set(frame.items.map((item) => item.id));
            for (const id of commentsFingerprintsRef.current.keys()) {
              if (!watchedIds.has(id)) commentsFingerprintsRef.current.delete(id);
            }
          }
          // For child sessions the server includes parent_session_id in the
          // wire item. Invalidate the parent's child_sessions cache so tree
          // views (SubagentsPanel, SubagentsGraphView) pick up status changes
          // without polling.
          const parentIds = new Set<string>();
          for (const item of frame.items) {
            if (item.parent_session_id) parentIds.add(item.parent_session_id);
          }
          for (const parentId of parentIds) {
            void queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(parentId) });
          }
          // Child sessions are watched through the tree caches, not the sidebar
          // list, so their frames always land in missingIds. Their own
          // child-sessions invalidation above is the refresh that matters; a
          // list refetch for them reconciles nothing.
          const childSessionIds = collectChildSessionIds(queryClient);
          for (const item of frame.items) {
            if (item.parent_session_id) childSessionIds.add(item.id);
          }
          const { missingIds, needsRefetch, orderStale } = applyItemsToCache(
            queryClient,
            frame.items,
            getCurrentUserId(),
            activeIdRef.current ?? undefined,
          );
          // A watched id absent from every page is a new session whose sort
          // position we can't place locally. Membership-affecting deltas
          // (archive flips, relabels, searched-title changes) need the same
          // server-side reconciliation.
          //
          // Skip the active session: its updated_at bumps on open before the
          // initial list fetch returns, so it lands in missingIds even though
          // it isn't a genuinely new session. Its data is covered by
          // useSession and it's pinned in the sidebar via ActiveChatOverride.
          // Skip child sessions too: they're off-list by construction and the
          // child-sessions cache above already carries their freshness.
          const sidebarMissingIds = missingIds.filter(
            (id) => id !== activeIdRef.current && !childSessionIds.has(id),
          );
          // Reconcile list membership when the open session's permission
          // changes (e.g. its owner shares it with the viewer directly, or
          // revokes a direct share while a link share remains), not on every
          // activity update. The refetch is authoritative, so the keep-alive
          // must not re-inject the row over it.
          const active = activeIdRef.current;
          const activeItem = active ? frame.items.find((item) => item.id === active) : undefined;
          let activePermissionChanged = false;
          if (active && activeItem) {
            const level = activeItem.permission_level ?? null;
            const seen = activePermissionRef.current;
            activePermissionChanged = seen?.id === active && seen.level !== level;
            activePermissionRef.current = { id: active, level };
            if (activePermissionChanged) unmarkRecentlyCreated(active);
          }
          if (sidebarMissingIds.length > 0 || needsRefetch || activePermissionChanged) {
            scheduleInvalidate();
          } else {
            reconciler.noteActivity();
            if (orderStale) scheduleOrderRefresh();
          }
          return;
        }
      }
    });

    // Recompute the watch-set from the cache whenever a conversations query
    // changes (initial fetch, fallback refetch, pagination, our own splices).
    let watchTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleWatch = () => {
      if (watchTimer !== null) return;
      watchTimer = setTimeout(() => {
        watchTimer = null;
        pushWatched();
      }, DEBOUNCE_MS);
    };

    pushWatched();
    const cache = queryClient.getQueryCache();
    const unsubscribeCache = cache.subscribe((event) => {
      const key = event.query.queryKey;
      // Recompute the watch-set when either the global list or a project
      // folder's list changes (fetch, pagination, splice) so newly loaded
      // folder members join the stream's watch-set.
      if (
        Array.isArray(key) &&
        (key[0] === "conversations" ||
          key[0] === "project-sessions" ||
          key[0] === "recent-sessions")
      ) {
        scheduleWatch();
      }
      // Also recompute when a child-sessions list loads so the tree nodes
      // join the watch-set and the server starts streaming their status.
      if (Array.isArray(key) && key[0] === "conversation" && key[2] === "child_sessions") {
        scheduleWatch();
      }
    });

    return () => {
      unsubscribeFrames();
      unsubscribeMutations();
      unsubscribeCache();
      reconciler.dispose();
      if (orderRefreshTimer !== null) clearTimeout(orderRefreshTimer);
      if (watchTimer !== null) clearTimeout(watchTimer);
      sessionUpdatesSocket.stop();
    };
  }, [identityReady, queryClient, pushWatched]);

  return children;
}
