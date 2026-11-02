import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export interface HostTerminal {
  terminal_id: string;
  elevated: boolean;
  created_at: number;
  attach_path: string;
}

export function hostTerminalsPath(hostId: string): string {
  return `/v1/hosts/${encodeURIComponent(hostId)}/terminals`;
}

export function useHostTerminals(hostId: string, enabled: boolean) {
  const client = useQueryClient();
  const queryKey = ["host-terminals", hostId];
  const terminals = useQuery({
    queryKey,
    queryFn: async ({ signal }): Promise<HostTerminal[]> => {
      const response = await authenticatedFetch(hostTerminalsPath(hostId), { signal });
      if (!response.ok) throw await apiErrorFromResponse(response);
      const body = (await response.json()) as { terminals: HostTerminal[] };
      return body.terminals;
    },
    enabled,
    retry: false,
    refetchInterval: enabled ? 3000 : false,
  });
  const create = useMutation({
    mutationFn: async (elevated: boolean): Promise<HostTerminal> => {
      const response = await authenticatedFetch(hostTerminalsPath(hostId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ elevated }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response);
      return response.json();
    },
    retry: false,
    onSuccess: (terminal) => {
      client.setQueryData<HostTerminal[]>(queryKey, (current = []) => [
        ...current.filter((item) => item.terminal_id !== terminal.terminal_id),
        terminal,
      ]);
      void client.invalidateQueries({ queryKey });
    },
  });
  const close = useMutation({
    mutationFn: async (terminalId: string): Promise<void> => {
      const response = await authenticatedFetch(
        `${hostTerminalsPath(hostId)}/${encodeURIComponent(terminalId)}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw await apiErrorFromResponse(response);
    },
    retry: false,
    onSuccess: (_, terminalId) => {
      client.setQueryData<HostTerminal[]>(queryKey, (current = []) =>
        current.filter((item) => item.terminal_id !== terminalId),
      );
      void client.invalidateQueries({ queryKey });
    },
  });
  return { terminals, create, close };
}
