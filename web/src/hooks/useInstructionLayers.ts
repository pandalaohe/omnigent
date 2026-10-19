import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type LayerScope = "shared" | "agent" | "host" | "independent" | "mother" | "child";
export interface LayerKey {
  scope: LayerScope;
  agent_key: string;
  host_id: string;
}
export function layerId(key: LayerKey) {
  return JSON.stringify([key.scope, key.agent_key, key.host_id]);
}
export interface InstructionLayer extends LayerKey {
  text: string;
  revision_id: string | null;
  updated_at: number | null;
  updated_by: string | null;
  chars: number;
  host_removed: boolean;
  stale_layers: string[];
}
export interface InstructionRouteSize {
  host_id: string;
  agent_key: string;
  total_chars: number;
  over_budget?: boolean;
  framework_unavailable?: boolean;
}
export interface InstructionLayers {
  data: InstructionLayer[];
  agents: { key: string; display_name: string }[];
  order: string[];
  route_sizes: InstructionRouteSize[];
  size_reminder_chars: number;
}
export interface InstructionPreview {
  subject: { name: string; harness: string | null };
  route: {
    host_id: string | null;
    agent_key: string | null;
    child: boolean;
    peer: boolean;
    spawn: boolean;
  };
  parts: ({
    kind: string;
    text: string | null;
    chars: number;
    sent: boolean;
    readonly: boolean;
  } & Partial<LayerKey> & { revision_id?: string })[];
  text: string;
  total_chars: number;
  framework_unavailable: boolean;
}
export interface InstructionRevision {
  id: string;
  text: string;
  created_at: number;
  created_by: string | null;
}

const KEY = ["instruction-layers"];
async function read<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { error?: { message?: unknown } };
      if (typeof body.error?.message === "string") message = body.error.message;
    } catch {
      /* Keep the HTTP status when there is no error body. */
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function useInstructionLayers() {
  return useQuery({
    queryKey: KEY,
    queryFn: ({ signal }) => read<InstructionLayers>("/v1/instruction-layers", { signal }),
    staleTime: 5_000,
  });
}
export function useInstructionPreview(params: URLSearchParams | null) {
  const query = params?.toString();
  return useQuery({
    queryKey: [...KEY, "preview", query],
    queryFn: ({ signal }) =>
      read<InstructionPreview>(`/v1/instruction-layers/preview?${query}`, { signal }),
    enabled: query !== undefined,
    staleTime: 5_000,
  });
}
export function useInstructionRevisions(layer: LayerKey) {
  const query = new URLSearchParams({ ...layer });
  return useQuery({
    queryKey: [...KEY, "revisions", layer],
    queryFn: ({ signal }) =>
      read<{ data: InstructionRevision[] }>(`/v1/instruction-layers/revisions?${query}`, {
        signal,
      }),
    staleTime: 5_000,
  });
}
export function useSaveInstructionLayer() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (layer: LayerKey & { text: string }) =>
      read<InstructionLayer & { over_budget_routes: InstructionRouteSize[] }>(
        "/v1/instruction-layers",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(layer),
        },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: KEY });
    },
  });
}
