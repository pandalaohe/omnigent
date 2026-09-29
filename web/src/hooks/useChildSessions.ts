import { useInfiniteQuery, useQuery, type QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { isTempConvId } from "@/lib/tempConversationId";

/**
 * Maximum depth of sub-agent nesting the Agents rail renders, counted
 * in levels below the root ("main") session: 1 = children,
 * 2 = grandchildren, 3 = great-grandchildren. Deeper descendants are
 * neither fetched nor rendered.
 */
export const MAX_TREE_DEPTH = 3;

export interface ChildSessionError {
  code: string;
  message: string;
}

/**
 * UI-facing child (sub-agent) session record.
 *
 * Mirrors the ``ChildSessionSummary`` schema returned by
 * ``GET /v1/sessions/{id}/child_sessions``. Only the fields the UI
 * renders or addresses are surfaced — extra wire fields are
 * tolerated and ignored.
 */
export interface ChildSessionInfo {
  /** Child conversation/session identifier, e.g. ``"conv_child123"``. */
  id: string;
  /** Full title, ``"{tool}:{session_name}"``, e.g. ``"researcher:auth"``. */
  title: string | null;
  /** Human-readable task-derived label, e.g. ``"Investigate auth flow"``. */
  task_summary: string | null;
  /** Sub-agent type prefix, e.g. ``"researcher"``. */
  tool: string | null;
  /** Sub-agent instance name suffix, e.g. ``"auth"``. */
  session_name: string | null;
  /** Session-scoped labels from the child conversation. */
  labels?: Record<string, string>;
  /** Status of the latest task, e.g. ``"completed"``. */
  current_task_status: string | null;
  /** Durable error details from the latest failed child run. */
  last_task_error?: ChildSessionError | null;
  /** True when the latest task is in an active (queued/in_progress) state. */
  busy: boolean;
  /** Historical running state exists, but the Host found no current activity proof. */
  activity_unverified?: boolean;
  /** Snapshot connectivity is uncertain; the task itself has not ended. */
  native_activity_unverified?: boolean;
  /**
   * Single-line preview of the most recent message in the child's
   * conversation, truncated to ~150 chars with a trailing ellipsis.
   * ``null`` when the child has no message items yet.
   */
  last_message_preview: string | null;
  /**
   * Number of approval / input prompts the child is currently blocked
   * on. ``> 0`` means the sub-agent is parked awaiting user input, and
   * the Agents rail renders an "awaiting input" badge for it.
   */
  pending_elicitations_count: number;
  /**
   * Model the intelligent router picked for this sub-agent, e.g.
   * ``"databricks-claude-sonnet-5"``. ``null``/absent when the child was
   * not routed (routing off, or a server that predates the field).
   */
  routed_model?: string | null;
  /**
   * Effective host: the child's own ``host_id``, else the nearest
   * host-bound ancestor's. ``null`` when no ancestor is host-bound.
   */
  host_id?: string | null;
  /** Effective cwd: own ``worktree ?? workspace``, else the nearest ancestor's. */
  cwd?: string | null;
  /**
   * Branch taken from the same row the effective cwd came from. ``null``
   * when that row recorded none — never mixed with another row's branch.
   */
  git_branch?: string | null;
  /** Resolved harness, e.g. ``"codex-native"``. Drives the badge vendor. */
  harness?: string | null;
  /** Bound agent row id. For native children this is the parent's row. */
  agent_id?: string | null;
  /** Bound agent row name; ``null`` when the row is missing. */
  agent_name?: string | null;
  /**
   * Bundled member identity when the child is a member of a saved bundle
   * (the bound agent row then belongs to the parent bundle).
   */
  sub_agent_name?: string | null;
  /** Whether the child is archived (belongs to the rail's past zone). */
  archived?: boolean;
  /** Epoch seconds the child entered the archive; ``null`` when active. */
  archived_at?: number | null;
  /** Epoch seconds the child was created (rail ordering). */
  created_at?: number | null;
  /**
   * Keep-warm state fed by a later change; ``"warm"``/``"cold"`` render the
   * row pill, ``null``/absent renders nothing.
   */
  warm_state?: "warm" | "cold" | null;
}

/**
 * Wire shape of a single entry in the ``child_sessions`` response.
 * Field set matches the server's ``ChildSessionSummary`` Pydantic
 * model. Extra wire fields are silently ignored.
 */
interface ChildSessionWire {
  id: string;
  title: string | null;
  task_summary?: string | null;
  tool: string | null;
  session_name: string | null;
  labels?: Record<string, string>;
  current_task_status: string | null;
  last_task_error?: ChildSessionError | null;
  busy: boolean;
  activity_unverified?: boolean;
  native_activity_unverified?: boolean;
  last_message_preview?: string | null;
  pending_elicitations_count?: number;
  routed_model?: string | null;
  host_id?: string | null;
  cwd?: string | null;
  git_branch?: string | null;
  harness?: string | null;
  agent_id?: string | null;
  agent_name?: string | null;
  sub_agent_name?: string | null;
  archived?: boolean;
  archived_at?: number | null;
  created_at?: number | null;
  warm_state?: string | null;
}

interface ChildSessionsResponse {
  object: "list";
  data: ChildSessionWire[];
  has_more?: boolean;
  last_id?: string | null;
}

interface ChildSessionsPage {
  data: ChildSessionInfo[];
  has_more: boolean;
  last_id: string | null;
}

function mapChildSession(row: ChildSessionWire): ChildSessionInfo {
  return {
    id: row.id,
    title: row.title,
    task_summary: row.task_summary ?? null,
    tool: row.tool,
    session_name: row.session_name,
    labels: row.labels ?? {},
    current_task_status: row.current_task_status,
    last_task_error: parseChildSessionError(row.last_task_error),
    busy: row.busy,
    activity_unverified: row.activity_unverified ?? false,
    ...(row.native_activity_unverified !== undefined
      ? { native_activity_unverified: row.native_activity_unverified === true }
      : {}),
    last_message_preview: row.last_message_preview ?? null,
    pending_elicitations_count: row.pending_elicitations_count ?? 0,
    routed_model: row.routed_model ?? null,
    host_id: row.host_id ?? null,
    cwd: row.cwd ?? null,
    git_branch: row.git_branch ?? null,
    harness: row.harness ?? null,
    agent_id: row.agent_id ?? null,
    agent_name: row.agent_name ?? null,
    sub_agent_name: row.sub_agent_name ?? null,
    archived: row.archived ?? false,
    archived_at: row.archived_at ?? null,
    created_at: row.created_at ?? null,
    warm_state: row.warm_state === "warm" || row.warm_state === "cold" ? row.warm_state : null,
  };
}

/**
 * TanStack Query key for a session's child sessions.
 *
 * Exported so the SSE handler can invalidate the same cache entry
 * on ``session.created`` events (live updates).
 */
export function childSessionsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "child_sessions"];
}

/**
 * Walk the cached child-session lists to test whether ``targetId`` is
 * a known descendant of ``rootId``.
 *
 * Synchronous and cache-only — never fetches. Used by AppShell's
 * sticky root resolution: when the user clicks a row the rail just
 * rendered, every ancestor's child list is already cached, so
 * membership here means "the rail listed this session under that
 * root" and the root can be held steady while the target's snapshot
 * loads. The past zone's infinite-query cache counts too: a row the
 * rail shows under Past is still one of the root's descendants.
 *
 * @param queryClient - The app QueryClient holding child-session caches.
 * @param rootId - Root session whose cached tree to walk, e.g. ``"conv_root"``.
 * @param targetId - Session to look for, e.g. ``"conv_grandchild"``.
 * @param maxDepth - Levels below the root to examine, e.g. ``MAX_TREE_DEPTH``.
 * @returns True when ``targetId`` appears in the cached tree under ``rootId``.
 */
export function cachedTreeContains(
  queryClient: QueryClient,
  rootId: string,
  targetId: string,
  maxDepth: number,
): boolean {
  let frontier = [rootId];
  for (let depth = 0; depth < maxDepth && frontier.length > 0; depth++) {
    const next: string[] = [];
    for (const id of frontier) {
      const active = queryClient.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(id)) ?? [];
      const past = queryClient.getQueryData<{ pages: ChildSessionsPage[] }>([
        ...childSessionsQueryKey(id),
        "past",
      ]);
      const children = [...active, ...(past?.pages.flatMap((page) => page.data) ?? [])];
      for (const child of children) {
        if (child.id === targetId) return true;
        next.push(child.id);
      }
    }
    frontier = next;
  }
  return false;
}

/**
 * Sentinel value used in place of a session id for the rail/panel's
 * "main" entry. The panel resolves it to the currently-viewed parent
 * conversation id at runtime so the same code path serves both main
 * and child sessions.
 */
export const MAIN_EXECUTION_LOG_KEY = "main";

/**
 * Stable tab id for an execution-log entry, used as the panel's
 * Tabs trigger value and as the message between the rail and the
 * panel. Format is ``executionLog:<id>``, where ``<id>`` is either
 * ``"main"`` (the parent session) or a child session id.
 */
export function executionLogTabKey(idOrMain: string): string {
  return `executionLog:${idOrMain}`;
}

function parseChildSessionError(value: unknown): ChildSessionError | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.code !== "string" || typeof record.message !== "string") return null;
  if (!record.code || !record.message) return null;
  return { code: record.code, message: record.message };
}

interface UseChildSessionsResult {
  children: ChildSessionInfo[];
  isLoading: boolean;
  error: Error | null;
}

/**
 * Fetch one page of a parent's child sessions.
 *
 * ``after`` is the cursor (child id) to resume from; the page reports
 * ``has_more``/``last_id`` so the caller can continue.
 */
async function fetchChildSessionPage(
  sessionId: string,
  params: { zone: "active" | "past"; limit: number; after?: string | null },
): Promise<ChildSessionsPage> {
  const query = new URLSearchParams({ zone: params.zone, limit: String(params.limit) });
  if (params.after) query.set("after", params.after);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions?${query.toString()}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as ChildSessionsResponse;
  // Children run on this session's runner: record the link so their
  // session-scoped requests key by this session's host before their own
  // snapshot loads.
  for (const row of json.data) setSessionParent(row.id, sessionId);
  const hasMore = json.has_more === true;
  const lastId = json.last_id ?? null;
  // A page that claims more without advancing would silently truncate the
  // list, so surface it as the rail's error state instead.
  if (hasMore && (!lastId || lastId === params.after)) {
    throw new Error("Malformed child-session pagination: has_more without a new cursor");
  }
  return { data: json.data.map(mapChildSession), has_more: hasMore, last_id: lastId };
}

/**
 * Fetch a parent's active (non-archived) child sessions — every page.
 *
 * The rail groups the whole active set by host/cwd, so a partial first
 * page is not enough: follow ``after`` while the server reports more,
 * with no cap (the expected scale is tens, and D13 is a load limit, not a
 * page size). Malformed pagination throws.
 *
 * Exported for unit testing of the HTTP-shape contract; production
 * code should call ``useChildSessions``.
 */
export async function fetchChildSessions(sessionId: string): Promise<ChildSessionInfo[]> {
  const all: ChildSessionInfo[] = [];
  const requested = new Set<string>();
  let after: string | null = null;
  for (;;) {
    // Each page's cursor comes from the previous response — inherently serial.
    // oxlint-disable-next-line no-await-in-loop
    const page = await fetchChildSessionPage(sessionId, { zone: "active", limit: 100, after });
    all.push(...page.data);
    if (!page.has_more || page.last_id === null) return all;
    // A cursor the server already served would loop forever (A → B → A …).
    if (requested.has(page.last_id)) {
      throw new Error(`Malformed child-session pagination: repeated cursor ${page.last_id}`);
    }
    requested.add(page.last_id);
    after = page.last_id;
  }
}

/**
 * Live child-session list for a conversation, served by
 * ``GET /v1/sessions/{id}/child_sessions``.
 *
 * The ``session.created`` handler in ``chatStore.ts`` invalidates
 * this query key on each spawn so newly-created child sessions
 * appear without waiting for the next poll or a manual refresh.
 *
 * :param conversationId: Parent session/conversation identifier,
 *     or ``null`` to disable the query.
 * :param pollMs: Optional poll interval in milliseconds. When set,
 *     the query refetches every ``pollMs`` ms (paused while the
 *     tab is backgrounded). The execution-logs panel passes a value
 *     here so the dropdown updates when new sub-agents spawn;
 *     callers that just need a snapshot (the rail card) can omit it.
 */
export function useChildSessions(
  conversationId: string | null,
  pollMs?: number | null,
): UseChildSessionsResult {
  const sessionId = isTempConvId(conversationId) ? null : conversationId;
  const { data, isLoading, error } = useQuery({
    queryKey:
      sessionId === null
        ? ["conversation", null, "child_sessions"]
        : childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId as string),
    enabled: sessionId !== null,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
    refetchInterval: pollMs ?? false,
  });
  return {
    children: data ?? [],
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

/** Page size for the past zone's manual "Load more" paging. */
export const PAST_CHILD_SESSIONS_PAGE_SIZE = 20;

interface UsePastChildSessionsResult {
  children: ChildSessionInfo[];
  isLoading: boolean;
  error: Error | null;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
}

/**
 * Archived children of a parent, newest-archived first, paged 20 at a time.
 *
 * The query lives under the active child key as ``[…, "past"]``, so every
 * existing invalidation of ``childSessionsQueryKey`` (spawn, archive move,
 * watch frame) also refreshes this list. ``enabled`` keeps the fetch off
 * until the past zone is expanded.
 */
export function usePastChildSessions(
  conversationId: string | null,
  enabled: boolean,
): UsePastChildSessionsResult {
  const sessionId = isTempConvId(conversationId) ? null : conversationId;
  const query = useInfiniteQuery({
    queryKey:
      sessionId === null
        ? ["conversation", null, "child_sessions", "past"]
        : [...childSessionsQueryKey(sessionId), "past"],
    queryFn: ({ pageParam }) =>
      fetchChildSessionPage(sessionId as string, {
        zone: "past",
        limit: PAST_CHILD_SESSIONS_PAGE_SIZE,
        after: pageParam,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    enabled: sessionId !== null && enabled,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
  });
  return {
    children: query.data?.pages.flatMap((page) => page.data) ?? [],
    isLoading: query.isLoading,
    error: (query.error as Error | null) ?? null,
    hasNextPage: query.hasNextPage,
    isFetchingNextPage: query.isFetchingNextPage,
    fetchNextPage: () => {
      void query.fetchNextPage();
    },
  };
}
