// The caller's recently-touched sessions, newest first. The route is a
// dedicated `/me` endpoint, so a server without it answers 404 — treated as a
// capability signal (the sidebar shows a note instead of retrying).

import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  applySessionTombstones,
  type Conversation,
  type ConversationsPage,
} from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse, RECENT_SESSIONS_TOUCHED_EVENT } from "@/lib/sessionsApi";

/** Thrown for the 404 an older server returns; callers render the old-server note. */
export class RecentSessionsUnavailableError extends Error {
  constructor() {
    super("Recent sessions need a newer server.");
    this.name = "RecentSessionsUnavailableError";
  }
}

async function fetchRecentSessions(count: number): Promise<Conversation[]> {
  const res = await authenticatedFetch(`/v1/me/recent-sessions?limit=${count}`);
  if (res.status === 404) throw new RecentSessionsUnavailableError();
  if (!res.ok) throw await apiErrorFromResponse(res);
  return applySessionTombstones((await res.json()) as ConversationsPage, true).data;
}

/**
 * The recent-sessions slice, one query per count. Deliberately outside the
 * `["conversations"]` key prefix so the list mutations' prefix sweeps leave it
 * alone (same isolation as the pinned key). Enabled whenever a recent section
 * exists — collapsed sections still need their members for the header marker.
 */
export function useRecentSessions(count: number, enabled: boolean) {
  const queryClient = useQueryClient();
  const query = useQuery<Conversation[]>({
    queryKey: ["recent-sessions", count],
    queryFn: () => fetchRecentSessions(count),
    // Disable the query once the capability error lands: a disabled query is
    // skipped by invalidateQueries, so an archive / delete caller's Recent
    // invalidation can't refetch the known-unavailable route. The error stays
    // in state for the old-server note.
    enabled: (recentQuery) =>
      enabled && !(recentQuery.state.error instanceof RecentSessionsUnavailableError),
    refetchOnWindowFocus: (queryState) =>
      !(queryState.state.error instanceof RecentSessionsUnavailableError),
    refetchInterval: (queryState) =>
      enabled && !(queryState.state.error instanceof RecentSessionsUnavailableError)
        ? 60_000
        : false,
    retry: (failureCount, error) =>
      error instanceof RecentSessionsUnavailableError ? false : failureCount < 3,
  });

  useEffect(() => {
    const onTouched = () => {
      if (query.error instanceof RecentSessionsUnavailableError) return;
      void queryClient.invalidateQueries({ queryKey: ["recent-sessions"] });
    };
    window.addEventListener(RECENT_SESSIONS_TOUCHED_EVENT, onTouched);
    return () => window.removeEventListener(RECENT_SESSIONS_TOUCHED_EVENT, onTouched);
  }, [queryClient, query.error]);

  return query;
}
