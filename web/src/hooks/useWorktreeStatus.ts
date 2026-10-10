import { skipToken, useQuery } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";

export type WorktreeState =
  "none" | "clean" | "dirty" | "unknown" | "protected" | "shared" | "removed";

export interface WorktreeFile {
  path: string;
  status: string;
}

export interface WorktreeOwnStatus {
  state: WorktreeState;
  reason: string | null;
  path: string | null;
  branch: string | null;
  merged: boolean | null;
  merge_target: string | null;
  files: WorktreeFile[];
}

export interface WorktreeAggregateStatus {
  state: WorktreeState;
  reason: string | null;
}

export interface WorktreeBlocker {
  session_id: string;
  title: string;
  state: WorktreeState;
  reason: string | null;
}

export interface SessionWorktreeStatus {
  own: WorktreeOwnStatus;
  aggregate: WorktreeAggregateStatus;
  blockers: WorktreeBlocker[];
  session_count: number;
}

const STATES: ReadonlySet<string> = new Set([
  "none",
  "clean",
  "dirty",
  "unknown",
  "protected",
  "shared",
  "removed",
]);

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function state(value: unknown): value is WorktreeState {
  return typeof value === "string" && STATES.has(value);
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function parseStatus(value: unknown): SessionWorktreeStatus {
  if (!record(value) || !record(value.own) || !record(value.aggregate)) {
    throw new Error("Invalid worktree status response");
  }
  const { own, aggregate, blockers, session_count } = value;
  if (
    !state(own.state) ||
    !nullableString(own.reason) ||
    !nullableString(own.path) ||
    !nullableString(own.branch) ||
    !(own.merged === null || typeof own.merged === "boolean") ||
    !nullableString(own.merge_target) ||
    !Array.isArray(own.files) ||
    !own.files.every(
      (file) => record(file) && typeof file.path === "string" && typeof file.status === "string",
    ) ||
    !state(aggregate.state) ||
    !nullableString(aggregate.reason) ||
    !Array.isArray(blockers) ||
    !blockers.every(
      (blocker) =>
        record(blocker) &&
        typeof blocker.session_id === "string" &&
        typeof blocker.title === "string" &&
        state(blocker.state) &&
        nullableString(blocker.reason),
    ) ||
    !Number.isSafeInteger(session_count) ||
    typeof session_count !== "number" ||
    session_count < 0
  ) {
    throw new Error("Invalid worktree status response");
  }
  return value as unknown as SessionWorktreeStatus;
}

/** An archive preflight can request a fresh read through the same validated route. */
export async function fetchSessionWorktreeStatus(
  id: string,
  refresh = false,
  signal?: AbortSignal,
): Promise<SessionWorktreeStatus> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8_000);
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    const url = `/v1/sessions/${encodeURIComponent(id)}/worktree-status${refresh ? "?refresh=true" : ""}`;
    const response = await authenticatedFetch(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`Worktree status fetch failed: HTTP ${response.status}`);
    return parseStatus(await response.json());
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export function useWorktreeStatus(sessionId: string | null | undefined) {
  const info = useServerInfo();
  const supported =
    info !== "loading" && "worktree_status" in info && info.worktree_status === true;
  const enabled = supported && Boolean(sessionId);
  const server = getOmnigentServerIdentity() ?? "unidentified";
  const generation = getOmnigentHostGeneration();
  const query = useQuery({
    queryKey: ["session-worktree-status", server, generation, sessionId],
    queryFn:
      enabled && sessionId
        ? ({ signal }) => fetchSessionWorktreeStatus(sessionId, false, signal)
        : skipToken,
    enabled,
    retry: false,
    staleTime: 0,
    refetchInterval: enabled ? 25_000 : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  return {
    ...query,
    supported,
    // React Query retains previous data after a failed refetch. A live safety
    // mark must never reuse a previous clean read as an archive-safe signal.
    data: enabled && !query.isError && !query.isFetching ? query.data : undefined,
  };
}
