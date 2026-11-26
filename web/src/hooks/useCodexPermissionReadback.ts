import { useEffect, useMemo } from "react";
import {
  CODEX_PERMISSION_OBSERVATION_LABEL_KEY,
  codexPermissionObservationFromSession,
  codexPermissionReadback,
} from "@/lib/codexApprovalMode";
import type { Session } from "@/lib/types";
import { invalidateCodexPermissionReadback, useChatStore } from "@/store/chatStore";

export function useCodexPermissionReadback(
  session: Pick<Session, "id" | "runnerId" | "labels"> | null,
  sessionId: string | null,
  canRead: boolean,
  offline: boolean,
) {
  const cached = useChatStore((state) =>
    sessionId ? state.codexPermissionReadbacks[sessionId] : undefined,
  );
  const observation = useMemo(
    () =>
      cached !== undefined ? cached.observation : codexPermissionObservationFromSession(session),
    [cached, session],
  );
  useEffect(() => {
    if (!canRead || !sessionId || session?.id !== sessionId || !observation) return;
    if (offline || observation.runner_id !== session.runnerId) {
      invalidateCodexPermissionReadback(
        sessionId,
        session.labels?.[CODEX_PERMISSION_OBSERVATION_LABEL_KEY],
      );
    }
  }, [canRead, sessionId, observation, session?.id, session?.runnerId, session?.labels, offline]);
  return canRead && session?.id === sessionId
    ? codexPermissionReadback(session, observation, { offline, stale: cached?.stale })
    : undefined;
}
