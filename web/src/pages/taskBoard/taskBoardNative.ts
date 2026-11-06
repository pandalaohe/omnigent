import { useEffect } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { resolveWebSocketUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import type { SystemStatusView } from "@/hooks/useSystemStatus";
import type { UsageOverview } from "@/pages/usageBoard/usageBoardData";
import { readBoardJson } from "./taskBoardData";

export interface BoardSession {
  id: string;
  title?: string | null;
  status: string;
  parent_session_id?: string | null;
  archived?: boolean;
  archived_at?: number | null;
  updated_at?: number;
  host_id?: string | null;
  host_online?: boolean | null;
  workspace?: string | null;
  git_branch?: string | null;
  pending_elicitations_count?: number;
  total_cost_usd?: number;
  todos?: { content: string; status: string; activeForm?: string }[];
}
export interface BoardSchedule {
  id: string;
  name: string;
  next_run_at?: string | null;
  state: string;
}
const cadence = () => (document.visibilityState === "visible" ? 60_000 : false);

async function readSessions(archived: boolean): Promise<BoardSession[]> {
  const rows: BoardSession[] = [];
  const params = new URLSearchParams({ kind: "any", limit: "200" });
  if (archived) {
    params.set("archived_only", "true");
    params.set("updated_after", String(Math.floor(Date.now() / 1000) - 72 * 3600));
  }
  const seen = new Set<string>();
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    const page = await readBoardJson<{
      data: BoardSession[];
      has_more: boolean;
      last_id: string | null;
    }>(`/v1/sessions?${params}`);
    rows.push(...page.data);
    if (!page.has_more) break;
    if (!page.last_id || seen.has(page.last_id)) throw new Error("Repeated session cursor");
    seen.add(page.last_id);
    params.set("after", page.last_id);
  }
  return rows;
}

export function useBoardSessions() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["taskBoard", "sessions"],
    queryFn: async () => {
      const [active, archived] = await Promise.all([readSessions(false), readSessions(true)]);
      return [...new Map([...active, ...archived].map((row) => [row.id, row])).values()];
    },
    refetchInterval: cadence,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const ids = JSON.stringify(
    (query.data ?? [])
      .filter((row) => !row.archived)
      .map((row) => row.id)
      .sort(),
  );
  useEffect(() => {
    if (ids === "[]" || typeof WebSocket === "undefined") return;
    let socket: WebSocket | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const connect = () => {
      socket = new WebSocket(resolveWebSocketUrl("/v1/sessions/updates"));
      socket.onopen = () =>
        socket?.send(JSON.stringify({ type: "watch", session_ids: JSON.parse(ids) }));
      socket.onmessage = (event) => {
        let frame: { type?: string };
        try {
          frame = JSON.parse(String(event.data));
        } catch {
          return;
        }
        if (["snapshot", "changed", "removed"].includes(frame.type ?? "")) {
          void client.invalidateQueries({ queryKey: ["taskBoard", "sessions"] });
          void client.invalidateQueries({ queryKey: ["taskBoard", "session"] });
        }
      };
      socket.onclose = () => {
        if (!stopped) timer = setTimeout(connect, 5000);
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [client, ids]);
  return query;
}

export function useBoardSessionDetails(ids: string[]) {
  return useQueries({
    queries: [...new Set(ids)].map((id) => ({
      queryKey: ["taskBoard", "session", id],
      queryFn: () =>
        readBoardJson<BoardSession>(
          `/v1/sessions/${encodeURIComponent(id)}?include_preview=true&include_usage=true&include_items=false&include_liveness=false&refresh_state=false`,
        ),
      refetchInterval: cadence,
      staleTime: 30_000,
      retry: false,
    })),
  });
}

export function useBoardSilentSessions(sessions: BoardSession[]) {
  const candidates = sessions.filter(
    (session) =>
      session.status === "running" &&
      !session.archived &&
      session.updated_at &&
      Date.now() / 1000 - session.updated_at >= 1200,
  );
  const queries = useQueries({
    queries: candidates.map((session) => ({
      queryKey: ["taskBoard", "lastItem", session.id, session.updated_at],
      queryFn: () =>
        readBoardJson<{ data: { type: string; status?: string }[] }>(
          `/v1/sessions/${encodeURIComponent(session.id)}/items?order=desc&limit=1`,
        ),
      refetchInterval: cadence,
      staleTime: 30_000,
      retry: false,
    })),
  });
  return candidates.filter(
    (_, index) =>
      queries[index].isSuccess && queries[index].data?.data[0]?.type !== "function_call",
  );
}

export function useBoardSystem() {
  return useQuery({
    queryKey: ["taskBoard", "system"],
    queryFn: () => readBoardJson<SystemStatusView>("/v1/system/status"),
    refetchInterval: cadence,
    retry: false,
  });
}
export function useBoardQuota() {
  return useQuery({
    queryKey: ["taskBoard", "quota"],
    queryFn: async () => {
      const response = await authenticatedFetch("/v1/me/usage");
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(`Quota read failed (${response.status})`);
      return response.json() as Promise<UsageOverview>;
    },
    refetchInterval: cadence,
    retry: false,
  });
}
export function useBoardSchedule() {
  return useQuery({
    queryKey: ["taskBoard", "schedule"],
    queryFn: () => readBoardJson<{ scheduled_tasks: BoardSchedule[] }>("/v1/scheduled-tasks"),
    refetchInterval: cadence,
    retry: false,
  });
}

/** Cycle-safe preorder; a missing parent keeps its child visible at the root. */
export function sessionTree(sessions: BoardSession[]): { session: BoardSession; depth: number }[] {
  const byId = new Map(sessions.map((session) => [session.id, session]));
  const children = new Map<string, BoardSession[]>();
  for (const session of sessions) {
    const parent = session.parent_session_id ?? "";
    children.set(parent, [...(children.get(parent) ?? []), session]);
  }
  const result: { session: BoardSession; depth: number }[] = [];
  const seen = new Set<string>();
  const visit = (session: BoardSession, depth: number) => {
    if (seen.has(session.id)) return;
    seen.add(session.id);
    result.push({ session, depth });
    for (const child of children.get(session.id) ?? []) visit(child, depth + 1);
  };
  for (const session of sessions) if (!byId.has(session.parent_session_id ?? "")) visit(session, 0);
  for (const session of sessions) visit(session, 0);
  return result;
}
