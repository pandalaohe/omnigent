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
    enabled,
    refetchOnWindowFocus: true,
    refetchInterval: enabled ? 60_000 : false,
    retry: (failureCount, error) =>
      error instanceof RecentSessionsUnavailableError ? false : failureCount < 3,
  });

  useEffect(() => {
    const onTouched = () => {
      void queryClient.invalidateQueries({ queryKey: ["recent-sessions"] });
    };
    window.addEventListener(RECENT_SESSIONS_TOUCHED_EVENT, onTouched);
    return () => window.removeEventListener(RECENT_SESSIONS_TOUCHED_EVENT, onTouched);
  }, [queryClient]);

  return query;
}
