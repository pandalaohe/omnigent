import { useQuery } from "@tanstack/react-query";
import { choiceRequest } from "./sessionChoiceData";

export function NewChatChoiceNotice({
  host,
  agent,
  model,
  enabled = true,
}: {
  host: string | null;
  agent: string | null;
  model?: string | null;
  enabled?: boolean;
}) {
  const notice = useQuery({
    queryKey: ["sessionChoiceNotice", host, agent, model],
    queryFn: ({ signal }) =>
      choiceRequest<{ notice: string | null }>(
        `/notice?${new URLSearchParams({ host_id: host!, agent_id: agent!, ...(model ? { model } : {}) })}`,
        { signal },
      ),
    enabled: enabled && !!host && !!agent,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  return enabled && notice.data?.notice && !notice.isError ? (
    <p className="text-xs text-muted-foreground" role="note">
      {notice.data.notice}
    </p>
  ) : null;
}
