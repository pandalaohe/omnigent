import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
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
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(true), 500);
    return () => window.clearTimeout(timer);
  }, [host, agent, model]);
  const notice = useQuery({
    queryKey: ["sessionChoiceNotice", host, agent, model],
    queryFn: ({ signal }) =>
      choiceRequest<{ notice: string | null }>(
        `/notice?${new URLSearchParams({ host_id: host!, agent_id: agent!, ...(model ? { model } : {}) })}`,
        { signal },
      ),
    enabled: enabled && settled && !!host && !!agent && !!model,
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
