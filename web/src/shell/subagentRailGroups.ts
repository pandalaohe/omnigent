import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  claudeNativeSubagentLabel,
  codexNativeSubagentLabel,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";

/**
 * The subset of a child row (or session snapshot) the rail's labels read.
 * A session snapshot carries camelCase aliases; callers normalize before use.
 */
export interface ChildSessionLike {
  id: string;
  title?: string | null;
  task_summary?: string | null;
  tool?: string | null;
  session_name?: string | null;
  sub_agent_name?: string | null;
  agent_name?: string | null;
  agent_id?: string | null;
  harness?: string | null;
  host_id?: string | null;
  labels?: Record<string, string>;
}

export interface ChildCwdGroup {
  cwd: string | null;
  children: ChildSessionInfo[];
}

export interface ChildHostGroup {
  hostId: string;
  count: number;
  cwdGroups: ChildCwdGroup[];
}

function titleHead(title: string | null | undefined): string | null {
  if (!title) return null;
  const colon = title.indexOf(":");
  return colon === -1 ? title : title.slice(0, colon) || null;
}

function titleSuffix(title: string | null | undefined): string | null {
  if (!title?.includes(":")) return null;
  return title.split(":").slice(1).join(":") || null;
}

/**
 * Primary label for a child row / the child page header.
 *
 * Native sub-agent wrappers keep the server-resolved ``tool`` (the Task
 * description / nickname / role) — their ``session_name`` is an opaque
 * correlation id. A session snapshot has no ``tool``, so the vendor label
 * helpers and the title head stand in for it. Everything else prefers the
 * name the mother gave the child (``session_name``, else the title suffix)
 * over the auto-generated task summary.
 */
export function childPrimaryLabel(child: ChildSessionLike): string {
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const nativeAgent = nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]);
  if (nativeAgent && !isUserAdded) {
    const vendorLabel =
      nativeAgent.key === "claude"
        ? claudeNativeSubagentLabel(child.labels, child.sub_agent_name)
        : nativeAgent.key === "codex"
          ? codexNativeSubagentLabel(child.labels)
          : titleHead(child.title);
    return child.tool ?? vendorLabel ?? child.title ?? child.id;
  }
  return (
    child.session_name ??
    titleSuffix(child.title) ??
    child.title ??
    child.task_summary ??
    child.tool ??
    child.id
  );
}

const LOCAL_HOST_GROUP = "local";

function newestCreated(rows: ChildSessionInfo[]): number {
  let newest = 0;
  for (const row of rows) newest = Math.max(newest, row.created_at ?? 0);
  return newest;
}

/**
 * Group top-level active children by host, then cwd, both newest-child
 * first; rows inside a cwd group are newest-created first. A hostless child
 * joins the root's host group (``"local"`` when the root has none too), and
 * a child with no recorded cwd falls into one "(unknown directory)" group.
 * Grandchildren are not grouped — they stay nested under their row.
 */
export function groupChildren(
  children: ChildSessionInfo[],
  rootHostId: string | null,
): ChildHostGroup[] {
  const hostBuckets = new Map<string, Map<string, ChildSessionInfo[]>>();
  for (const child of children) {
    const hostId = child.host_id ?? rootHostId ?? LOCAL_HOST_GROUP;
    const cwd = child.cwd ?? "";
    let cwdBuckets = hostBuckets.get(hostId);
    if (cwdBuckets === undefined) {
      cwdBuckets = new Map<string, ChildSessionInfo[]>();
      hostBuckets.set(hostId, cwdBuckets);
    }
    const rows = cwdBuckets.get(cwd);
    if (rows === undefined) cwdBuckets.set(cwd, [child]);
    else rows.push(child);
  }

  const byNewest = (left: ChildSessionInfo[], right: ChildSessionInfo[]) =>
    newestCreated(right) - newestCreated(left);

  return [...hostBuckets.entries()]
    .sort(([, a], [, b]) => {
      const newestA = Math.max(0, ...[...a.values()].map(newestCreated));
      const newestB = Math.max(0, ...[...b.values()].map(newestCreated));
      return newestB - newestA;
    })
    .map(([hostId, cwdBuckets]) => {
      const cwdGroups = [...cwdBuckets.entries()]
        .sort(([, a], [, b]) => byNewest(a, b))
        .map(([cwd, rows]) => ({
          cwd: cwd === "" ? null : cwd,
          children: [...rows].sort(
            (left, right) => (right.created_at ?? 0) - (left.created_at ?? 0),
          ),
        }));
      return {
        hostId,
        count: cwdGroups.reduce((total, group) => total + group.children.length, 0),
        cwdGroups,
      };
    });
}

const HOME_PATH = /^(\/Users\/[^/]+|\/home\/[^/]+)(\/.*)?$/;

/**
 * Shorten a path for a group header or chip: `/Users/<u>/…` and
 * `/home/<u>/…` collapse to `~/…`; anything still over 40 characters keeps
 * only its last two segments behind a `…/`.
 */
export function shortenPath(path: string): string {
  if (!path) return path;
  const home = HOME_PATH.exec(path);
  let shortened = home ? `~${home[2] ?? ""}` : path;
  if (shortened.length > 40) {
    const segments = shortened.split("/").filter(Boolean);
    shortened = `…/${segments.slice(-2).join("/")}`;
  }
  return shortened;
}
