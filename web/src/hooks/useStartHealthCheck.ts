// The one-click health check: snapshot the monitor into a brief and open an
// ordinary project session on the configured ops host with the prompt and the
// brief as its first message.
//
// The create body carries no `agent_id` — the server resolves the project's
// default agent for that host. The session is created first and the first
// message posted after, so a failed post leaves a session the notice links to.

import { useCallback, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";
import { ApiError, createProjectSession, postEvent } from "@/lib/sessionsApi";
import { useHosts } from "@/hooks/useHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useSystemStatusSettings } from "@/hooks/useSystemStatus";
import type { Session } from "@/lib/types";

export interface HealthCheckNotice {
  kind: "unset" | "host_offline" | "error";
  message: string;
  sessionId?: string;
}

interface BriefResponse {
  text: string;
  generated_at: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function briefErrorMessage(res: Response): Promise<string> {
  let message = `${res.status} ${res.statusText}`.trim();
  try {
    const body = (await res.json()) as { error?: { message?: unknown }; detail?: unknown };
    if (typeof body.error?.message === "string") message = body.error.message;
    else if (typeof body.detail === "string" && body.detail) message = body.detail;
  } catch {
    /* Non-JSON error body: the status fallback stands. */
  }
  return message;
}

/** Local-time `YYYY-MM-DD HH:MM` for the session title. */
function healthCheckTitle(now: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `Health check ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

/**
 * Open the ops session and send the prompt + brief. Admin-only in practice:
 * the settings and brief routes are admin-gated, so the settings fetch only
 * starts for admins.
 */
export function useStartHealthCheck(): {
  start: () => Promise<void>;
  pending: boolean;
  notice: HealthCheckNotice | null;
} {
  const isAdmin = useIsAdmin();
  const settings = useSystemStatusSettings({ enabled: isAdmin });
  const hosts = useHosts({ enabled: isAdmin });
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState<HealthCheckNotice | null>(null);
  // A ref, not the state: two clicks in the same tick both see `pending: false`.
  const inFlight = useRef(false);

  const start = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setNotice(null);
    try {
      const healthCheck = settings.data?.health_check;
      const projectId = healthCheck?.project_id ?? null;
      const hostId = healthCheck?.host_id ?? null;
      if (projectId === null || hostId === null) {
        setNotice({
          kind: "unset",
          message: "Set the ops project and host in Settings → System status.",
        });
        return;
      }
      const host = hosts.data?.find((candidate) => candidate.host_id === hostId);
      if (host !== undefined && host.status === "offline") {
        setNotice({
          kind: "host_offline",
          message: `The ops host "${host.name}" is offline. Reconnect it, then try again.`,
        });
        return;
      }

      let brief: BriefResponse;
      try {
        const res = await authenticatedFetch("/v1/system/brief");
        if (!res.ok) throw new Error(await briefErrorMessage(res));
        brief = (await res.json()) as BriefResponse;
      } catch (error) {
        setNotice({ kind: "error", message: errorMessage(error) });
        return;
      }

      let session: Session;
      try {
        session = await createProjectSession({ projectId, hostId, title: healthCheckTitle() });
      } catch (error) {
        const ownership =
          error instanceof ApiError && (error.status === 403 || error.status === 404)
            ? " The ops project and host must belong to you."
            : "";
        setNotice({ kind: "error", message: `${errorMessage(error)}${ownership}` });
        return;
      }

      const prompt = healthCheck?.prompt ?? settings.data?.default_health_check_prompt ?? "";
      try {
        await postEvent(session.id, {
          type: "message",
          data: {
            role: "user",
            content: [{ type: "input_text", text: `${prompt}\n\n${brief.text}` }],
          },
        });
      } catch (error) {
        setNotice({ kind: "error", message: errorMessage(error), sessionId: session.id });
        return;
      }

      navigate(`/c/${session.id}`);
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }, [hosts.data, navigate, settings.data]);

  return { start, pending, notice };
}
