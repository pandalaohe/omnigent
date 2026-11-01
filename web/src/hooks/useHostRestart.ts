import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export interface HostRestartPreview {
  preview_id: string;
  host_id: string;
  supervisor: string;
  running_sessions: string[];
  runner_count: number;
  caller_on_host: boolean;
}

export type HostRestartPhase = "draining" | "stopping" | "waiting_reconnect" | "done" | "failed";

export interface HostRestartStatus {
  phase: HostRestartPhase;
  reason: string | null;
  offline_seconds: number | null;
  version: string | null;
  cut_sessions: string[];
}

function restartUrl(hostId: string) {
  return `/v1/hosts/${encodeURIComponent(hostId)}/restart`;
}

async function requestRestart(hostId: string, previewId: string | null) {
  const response = await authenticatedFetch(restartUrl(hostId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dry_run: previewId === null,
      preview_id: previewId,
      force: false,
      drain_timeout_s: 120,
      requested_by_session_id: null,
    }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export function useHostRestart(hostId: string, requestId: string | null) {
  const client = useQueryClient();
  const preview = useMutation({
    mutationFn: async (): Promise<HostRestartPreview> => requestRestart(hostId, null),
    retry: false,
  });
  const start = useMutation({
    mutationFn: async (
      previewId: string,
    ): Promise<{ request_id: string; phase: HostRestartPhase }> =>
      requestRestart(hostId, previewId),
    retry: false,
  });
  const status = useQuery({
    queryKey: ["host-restart", hostId, requestId],
    queryFn: async ({ signal }): Promise<HostRestartStatus> => {
      const response = await authenticatedFetch(
        `${restartUrl(hostId)}/${encodeURIComponent(requestId!)}`,
        { signal },
      );
      if (!response.ok) throw await apiErrorFromResponse(response);
      return response.json();
    },
    enabled: requestId !== null,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.phase === "done" || query.state.data?.phase === "failed" ? false : 2000,
  });
  const phase = status.data?.phase;
  useEffect(() => {
    if (phase === "done" || phase === "failed") {
      void client.invalidateQueries({ queryKey: ["hosts"] });
      void client.invalidateQueries({ queryKey: ["system-status"] });
    }
  }, [client, phase]);
  return { preview, start, status };
}
