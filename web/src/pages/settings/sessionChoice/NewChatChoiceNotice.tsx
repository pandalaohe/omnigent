import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import {
  SESSION_COLLAB_CHANGED_EVENT,
  SESSION_COLLAB_STORAGE_KEY,
} from "@/lib/sessionCollabPreferences";
import { choiceRequest, useSessionChoiceDocument } from "./sessionChoiceData";

function syncedChoiceEnabled() {
  try {
    return (
      JSON.parse(window.localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "null")?.sessionChoice
        ?.enabled === true
    );
  } catch {
    return false;
  }
}
function subscribeChoice(onChange: () => void) {
  window.addEventListener(SESSION_COLLAB_CHANGED_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(SESSION_COLLAB_CHANGED_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

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
  const document = useSessionChoiceDocument({ enabled: false });
  const syncedEnabled = useSyncExternalStore(subscribeChoice, syncedChoiceEnabled, () => false);
  const active = document.data ? document.data.settings?.enabled === true : syncedEnabled;
  const notice = useQuery({
    queryKey: ["sessionChoiceNotice", host, agent, model],
    queryFn: ({ signal }) =>
      choiceRequest<{ notice: string | null }>(
        `/notice?${new URLSearchParams({ host_id: host!, agent_id: agent!, ...(model ? { model } : {}) })}`,
        { signal },
      ),
    enabled: enabled && active && !!host && !!agent,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  return enabled && active && notice.data?.notice && !notice.isError ? (
    <p className="text-xs text-muted-foreground" role="note">
      {notice.data.notice}
    </p>
  ) : null;
}
