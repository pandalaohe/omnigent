import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Agent } from "./useAgents";
import { useSettingsPageCursor } from "./useSettingsPageCursor";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { SETTINGS_PAGE_SIZE } from "@/lib/settingsPagination";
import { isStaleCursorError } from "@/lib/staleCursor";
import { buildAgentBundle, type AgentBundleInput } from "@/lib/agentBundle";
import {
  getAgentConfig,
  patchAgentConfig,
  type AgentConfigPatch,
  installAgentBundle,
} from "@/lib/agentsApi";

export interface ManagedAgent extends Agent {
  harness: string | null;
  version: number;
  created_at: number;
  updated_at: number | null;
  builtin: boolean;
  skills: { name: string; description: string }[];
}

interface ManagedAgentDetail extends ManagedAgent {
  user_owned?: boolean;
}

interface AgentPage {
  data: ManagedAgent[];
  has_more: boolean;
  last_id: string | null;
}

/** Management keeps every ID, including same-name agents omitted by the picker. */
export function useCustomAgents(scope: "server" | "user", enabled: boolean) {
  const client = useQueryClient();
  const { cursor, page, previous, next, restart } = useSettingsPageCursor();
  const query = useQuery({
    queryKey: ["settings-agents", scope, cursor],
    queryFn: async ({ signal }): Promise<AgentPage> => {
      const params = new URLSearchParams({ limit: String(SETTINGS_PAGE_SIZE) });
      if (scope === "user") params.set("scope", scope);
      if (cursor) params.set("after", cursor);
      const res = await authenticatedFetch(`/v1/agents?${params}`, { signal });
      if (!res.ok) throw await apiErrorFromResponse(res);
      return res.json();
    },
    enabled,
  });
  const lostCursor = !!cursor && isStaleCursorError(query.error ?? query.failureReason);
  useEffect(() => {
    if (!lostCursor || !enabled) return;
    void client.invalidateQueries({ queryKey: ["settings-agents", scope], refetchType: "none" });
    restart();
  }, [client, enabled, lostCursor, restart, scope]);
  const hasNext = query.data?.has_more === true;
  return {
    query,
    pager: {
      page,
      hasNext,
      busy: query.isFetching,
      onPrevious: previous,
      onNext: () => {
        if (hasNext) next(query.data?.last_id);
      },
    },
  };
}

export function useCustomAgent(id: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["settings-agents", "detail", id],
    queryFn: async ({ signal }): Promise<ManagedAgentDetail | null> => {
      const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(id!)}`, { signal });
      if (res.status === 404) return null;
      if (!res.ok) throw await apiErrorFromResponse(res);
      return res.json();
    },
    enabled: enabled && id !== undefined,
    retry: false,
  });
}

export function useSaveCustomAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: AgentBundleInput) =>
      installAgentBundle(await buildAgentBundle(input)),
    onSuccess: () => refreshAgents(client),
  });
}

export function useDeleteCustomAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, force }: { id: string; force: boolean }): Promise<string | null> => {
      const res = await authenticatedFetch(
        `/v1/agents/${encodeURIComponent(id)}${force ? "?force=true" : ""}`,
        { method: "DELETE" },
      );
      if (res.status === 409) {
        const body = await res
          .clone()
          .json()
          .catch(() => null);
        if (body?.error?.code === "agent_in_use" && typeof body.sessions_in_use === "string") {
          return body.sessions_in_use;
        }
      }
      if (!res.ok) throw await apiErrorFromResponse(res);
      return null;
    },
    onSuccess: (inUse) => {
      if (inUse === null) refreshAgents(client);
    },
  });
}

function refreshAgents(client: ReturnType<typeof useQueryClient>) {
  for (const key of [
    "settings-agents",
    "available-agents-user",
    "available-agents-catalog",
    "available-agents",
    "session-agent",
    "conversations",
  ]) {
    void client.invalidateQueries({ queryKey: [key] });
  }
}

export function useCustomAgentConfig(id: string) {
  return useQuery({
    queryKey: ["settings-agents", "config", id],
    queryFn: ({ signal }) => getAgentConfig(id, signal),
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

export function usePatchCustomAgent(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ etag, patch }: { etag: string | null; patch: AgentConfigPatch }) =>
      patchAgentConfig(id, etag, patch),
    retry: false,
    gcTime: 0,
    onSuccess: () => refreshAgents(client),
  });
}
