import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { harnessSection, reportedOnAnyHost, type RegistryItem } from "@/hooks/useAgentRegistry";
import type { Host } from "@/hooks/useHosts";
import type { HarnessCapability } from "@/lib/agentLabels";
import type { InstructionGridLayout } from "@/lib/instructionGridPreferences";
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
  agents: {
    key: string;
    display_name: string;
    harnesses?: string[];
    subject?: { name: string; harness: string };
  }[];
  order: string[];
  route_sizes: InstructionRouteSize[];
  size_reminder_chars: number;
}
/** Picker membership comes from the same descriptors that validate saved layer keys. */
export function instructionRegistryItems(
  agents: InstructionLayers["agents"],
  capabilities: Record<string, HarnessCapability>,
  hosts: readonly Host[],
): RegistryItem[] {
  return agents.map((agent) => {
    const harnesses =
      agent.harnesses ??
      Object.keys(capabilities).filter((h) => capabilities[h].native_agent_key === agent.key);
    const harness = harnesses[0] ?? null;
    const capability = (h: string | null) =>
      h ? (capabilities[h] ?? (h.startsWith("acp:") ? capabilities.acp : undefined)) : undefined;
    return {
      key: agent.key,
      label: agent.display_name,
      harness,
      source: "builtin",
      description: null,
      section: harnessSection(capability(harness)?.integration_mode, harness),
      deployed:
        harnesses.length === 0 || harnesses.some((h) => reportedOnAnyHost(h, capability(h), hosts)),
    };
  });
}

export function hasInstructionText(text: string | null | undefined): boolean {
  // eslint-disable-next-line no-control-regex -- Match Python str.strip() control whitespace.
  return /[^\p{White_Space}\u001c-\u001f]/u.test(text ?? "");
}

export function instructionGridAxes(
  data: InstructionLayers,
  hosts: Host[],
  layout: InstructionGridLayout,
) {
  const hostIds = new Set(hosts.map((host) => host.host_id));
  const agentKeys = new Set(data.agents.map((agent) => agent.key));
  const agentLayers = new Map<string, InstructionLayer[]>();
  const hostLayers = new Map<string, InstructionLayer[]>();
  const heldHosts = new Set<string>();
  const append = (map: Map<string, InstructionLayer[]>, id: string, layer: InstructionLayer) => {
    map.set(id, [...(map.get(id) ?? []), layer]);
  };
  for (const layer of data.data) {
    if (!hasInstructionText(layer.text)) continue;
    const existingHost = hostIds.has(layer.host_id) && !layer.host_removed;
    if (layer.scope === "agent" || (layer.scope === "independent" && existingHost)) {
      append(agentLayers, layer.agent_key, layer);
    }
    if (existingHost && (layer.scope === "host" || layer.scope === "independent")) {
      append(hostLayers, layer.host_id, layer);
      if (layer.scope === "host" || agentKeys.has(layer.agent_key)) heldHosts.add(layer.host_id);
    }
  }
  const ordered = <T>(items: T[], ids: string[], key: (item: T) => string, held: Set<string>) => {
    const byId = new Map(items.map((item) => [key(item), item]));
    const listed = new Set(ids);
    return [
      ...ids.flatMap((id) => (byId.has(id) ? [byId.get(id)!] : [])),
      ...items.filter((item) => !listed.has(key(item)) && held.has(key(item))),
    ];
  };
  return {
    agents: ordered(data.agents, layout.agents, (agent) => agent.key, new Set(agentLayers.keys())),
    hosts: ordered(hosts, layout.hosts, (host) => host.host_id, heldHosts),
    agentLayers,
    hostLayers,
  };
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
