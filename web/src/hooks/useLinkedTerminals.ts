import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import type { AgentTier, TerminalInfo } from "@/lib/terminals";

export interface LinkedTerminal {
  terminal_id: string;
  host_id: string;
  host_name: string;
  elevated: boolean;
  instance_id: string;
  agent_tier: AgentTier;
}

export const linkedTerminalsKey = (sessionId: string) => [
  "conversation",
  sessionId,
  "host-terminals",
];
export const linkedTerminalsPath = (sessionId: string) =>
  `/v1/sessions/${encodeURIComponent(sessionId)}/host-terminals`;

export function linkedTerminalInfo(t: LinkedTerminal): TerminalInfo {
  return {
    id: `host:${encodeURIComponent(t.host_id)}:${t.terminal_id}`,
    name: "shell",
    session: t.terminal_id.slice(0, 8),
    running: true,
    hostId: t.host_id,
    hostName: t.host_name,
    hostTerminalId: t.terminal_id,
    instanceId: t.instance_id,
    elevated: t.elevated,
    agentTier: t.agent_tier,
  };
}

export function linkedTerminalTarget(id: string): { hostId: string; terminalId: string } | null {
  const match = /^host:([^:]+):([a-f0-9]{32})$/.exec(id);
  return match ? { hostId: decodeURIComponent(match[1]), terminalId: match[2] } : null;
}

export function useLinkedTerminals(sessionId: string | null) {
  const client = useQueryClient();
  return useQuery({
    queryKey: linkedTerminalsKey(sessionId ?? ""),
    enabled: Boolean(sessionId),
    retry: false,
    queryFn: async ({ signal }) => {
      const res = await authenticatedFetch(linkedTerminalsPath(sessionId!), { signal });
      if (res.status === 404) return [];
      if (!res.ok) throw await apiErrorFromResponse(res);
      const body = (await res.json()) as { terminals: LinkedTerminal[]; complete: boolean };
      const incoming = body.terminals.map(linkedTerminalInfo);
      if (body.complete) return incoming;
      const previous = client.getQueryData<TerminalInfo[]>(linkedTerminalsKey(sessionId!)) ?? [];
      return [...new Map([...previous, ...incoming].map((t) => [t.id, t])).values()];
    },
    refetchInterval: 15_000,
  });
}

export function useCreateLinkedTerminal(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { host_id: string; elevated: boolean; agent_tier: AgentTier }) => {
      const res = await authenticatedFetch(linkedTerminalsPath(sessionId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw await apiErrorFromResponse(res);
      return linkedTerminalInfo((await res.json()) as LinkedTerminal);
    },
    onSuccess: (terminal) =>
      client.setQueryData<TerminalInfo[]>(linkedTerminalsKey(sessionId), (current = []) => [
        ...current.filter((t) => t.id !== terminal.id),
        terminal,
      ]),
  });
}
