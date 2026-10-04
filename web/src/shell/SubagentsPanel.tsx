// Subagents tab content for the right-side rail. Renders the session
// tree under the root conversation: a "main" row, the active zone
// (top-level children grouped host → cwd with grandchildren nested
// under their row, depth ≤ MAX_TREE_DEPTH), and the collapsed past
// zone of archived children. The user can move between any agents in
// the tree without leaving the rail.
//
// The active session may itself be a descendant (the user clicked
// into a sub-agent). The rail still renders the tree from the
// top-level root, with the active row highlighted. AppShell resolves
// the root id (walking the parent chain) and passes it as
// ``rootSessionId``.
//
// Each row is a Link to the target conversation page so cmd/middle-
// click opens it in a new tab, matching the sidebar's behavior.

import { Fragment, lazy, Suspense, useMemo, useState } from "react";
import type { ComponentType, ReactNode, SVGProps } from "react";
import {
  BookOpenIcon,
  BotIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleDotIcon,
  CircleHelpIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CircleStopIcon,
  Code2Icon,
  CompassIcon,
  CornerDownRightIcon,
  EllipsisIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ListIcon,
  NetworkIcon,
  PauseIcon,
  PlusIcon,
  ScanSearchIcon,
  SearchIcon,
  UnplugIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { RailAgentBadge, childAgentDisplay, useChildAgentBadge } from "@/components/RailAgentBadge";
import { RunningDot } from "@/components/RunningDot";
import { shortModelName } from "@/components/CostRoutingControl";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  MAX_TREE_DEPTH,
  useChildSessions,
  usePastChildSessions,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";
import { useStopSession } from "@/hooks/useConversations";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useHostColorPreferences } from "@/hooks/useHostColorPreferences";
import { useSession } from "@/hooks/useSession";
import { hostColor, hostColorStyle, hostDisplayName } from "@/lib/hostColors";
import {
  nativeCodingAgentForSubagentWrapper,
  nativeCodingAgentForWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import type { Session, SessionItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  childPrimaryLabel,
  groupChildren,
  shortenPath,
  type ChildHostGroup,
  type ChildSessionLike,
} from "./subagentRailGroups";
import { childStatus, type AgentActivity, type AgentStatus } from "./subagentStatus";
import { AddAgentDialog } from "./AddAgentDialog";
import { ReconcileSubagentsButton } from "./ReconcileSubagentsButton";

const SubagentsGraphView = lazy(() =>
  import("./SubagentsGraphView").then((m) => ({ default: m.SubagentsGraphView })),
);

const CODEX_NATIVE_SUBAGENT_WRAPPER = "codex-native-ui-subagent";
const ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER = "antigravity-native-ui-subagent";
const CODEX_NATIVE_SUBAGENT_ROLE_LABEL = "omnigent.codex_native.agent_role";
const ANTIGRAVITY_NATIVE_SUBAGENT_ROLE_LABEL = "omnigent.antigravity_native.agent_role";

interface SubagentsPanelProps {
  /** The conversation currently rendered in main. Used only to
   *  highlight the active row. */
  conversationId: string;
  /** Root (parent) session whose children populate the list. When the
   *  user is on a top-level session this is the active id; when on a
   *  child it is the child's parent id. AppShell resolves this from
   *  ``activeSession.parentSessionId``. */
  rootSessionId: string;
}

type ViewMode = "list" | "graph";
interface StopTarget {
  id: string;
  label: string;
}

const UNKNOWN_DIRECTORY = "(unknown directory)";

/** Normalize a session snapshot to the child-row shape the shared helpers read. */
function sessionLike(session: Session | null, fallbackId: string): ChildSessionLike {
  return {
    id: session?.id ?? fallbackId,
    title: session?.title,
    sub_agent_name: session?.subAgentName,
    agent_name: session?.agentName,
    agent_id: session?.agentId,
    agent_template_id: session?.agentTemplateId,
    harness: session?.harness,
    host_id: session?.hostId,
    labels: session?.labels ?? {},
  };
}

function hostNameForHost(hostId: string | null | undefined, hosts: Map<string, Host>): string {
  return hostDisplayName(hostId, hostId ? hosts.get(hostId) : undefined);
}

type AgentRowIcon = ComponentType<SVGProps<SVGSVGElement>>;

// Pi children are scaffold (no wrapper label); the spawn title's agent-type head (``tool``) is the signal.
const PI_AGENT_NAME = "pi";

/**
 * Map a sub-agent type label to a category icon so a mix of agents reads by
 * role at a glance (Claude Code spawns many same-type "Explore" agents — the
 * icon distinguishes roles; the preview line below distinguishes instances).
 * Category icons are monochrome — the row applies the muted color; the
 * fallback is the generic bot icon.
 *
 * @param tool - The agent type, e.g. ``"Explore"`` or ``"researcher"``;
 *   ``null`` when the child carries no type.
 * @returns An SVG icon component.
 */
export function iconForAgentType(tool: string | null): AgentRowIcon {
  const t = (tool ?? "").toLowerCase();
  if (t.includes("explore")) return SearchIcon;
  if (t.includes("research")) return BookOpenIcon;
  if (t.includes("plan") || t.includes("architect")) return CompassIcon;
  if (t.includes("review")) return ScanSearchIcon;
  if (t.includes("test")) return FlaskConicalIcon;
  if (t.includes("doc") || t.includes("writ")) return FileTextIcon;
  if (
    t.includes("code") ||
    t.includes("eng") ||
    t.includes("dev") ||
    t.includes("front") ||
    t.includes("back")
  ) {
    return Code2Icon;
  }
  return BotIcon;
}

/**
 * Pick the harness identity for coding child sessions when the summary
 * carries enough metadata. The shared harness-selector icon component uses
 * this identity to render the same color variant in the Agents panel.
 *
 * Only full native sessions get the harness glyph. *Sub-agent* wrapper
 * children (``…-subagent``) deliberately fall through to the role icons
 * (and the generic bot fallback) — a native session's sub-agents are all the
 * same brand, so repeating the logo down the tree says nothing, while
 * role icons distinguish what each one is doing.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The agent identity used by the harness selector, or ``null``.
 */
function harnessAgentForChild(
  child: ChildSessionInfo,
): { name: string; harness: string | null } | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  if (nativeAgent) {
    return { name: nativeAgent.agentName, harness: nativeAgent.harness };
  }
  // Exact match — substring checks would false-match names like "pipeline".
  if (child.tool === PI_AGENT_NAME) {
    return { name: "pi-native-ui", harness: "pi-native" };
  }
  return null;
}

/**
 * A harness sub-agent mirror: a child spawned inside a native CLI (Claude
 * Task tool, Codex collab thread, …). These render the upstream sub-agent row
 * (role icon + connector, no badge / host colour / warm pill) and are split
 * into the root's own "Subagents" zone rather than the ACTIVE zone.
 */
function isHarnessSubagent(child: ChildSessionInfo): boolean {
  return nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]) != null;
}

export function SubagentsPanel({ conversationId, rootSessionId }: SubagentsPanelProps) {
  const { children, isLoading, error } = useChildSessions(rootSessionId);
  const { session: rootSession } = useSession(rootSessionId);
  const { data: hosts } = useHosts();
  const [addOpen, setAddOpen] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [collapsedRows, setCollapsedRows] = useState<Record<string, boolean>>({});
  const [collapsedHosts, setCollapsedHosts] = useState<Record<string, boolean>>({});
  const [collapsedCwds, setCollapsedCwds] = useState<Record<string, boolean>>({});
  const [pastExpanded, setPastExpanded] = useState(false);
  const [subagentsExpanded, setSubagentsExpanded] = useState(false);
  const [stopTarget, setStopTarget] = useState<StopTarget | null>(null);
  const past = usePastChildSessions(rootSessionId, pastExpanded);
  const toggleCollapsedRow = (id: string) => {
    setCollapsedRows((current) => ({ ...current, [id]: !current[id] }));
  };
  const hostsById = useMemo(
    () => new Map((hosts ?? []).map((host) => [host.host_id, host])),
    [hosts],
  );
  const hostNameFor = (hostId: string | null | undefined) => hostNameForHost(hostId, hostsById);
  // The root's own harness-subagent mirrors get their own collapsed zone;
  // the ACTIVE zone, its counts, and the host/cwd grouping see real
  // sessions only.
  const sessionChildren = useMemo(
    () => children.filter((child) => !isHarnessSubagent(child)),
    [children],
  );
  const rootSubagents = useMemo(() => children.filter(isHarnessSubagent), [children]);
  const hostGroups = useMemo(
    () => groupChildren(sessionChildren, rootSession?.hostId ?? null),
    [sessionChildren, rootSession?.hostId],
  );
  const warmCount = sessionChildren.filter((child) => child.warm_state === "warm").length;
  const coldCount = sessionChildren.filter((child) => child.warm_state === "cold").length;
  const showsWarmCounts = sessionChildren.some((child) => child.warm_state != null);
  const subagentStatuses = rootSubagents.map(childStatus);
  const runningSubagentCount = subagentStatuses.filter(
    (status) => status.activity === "working" || status.activity === "launching",
  ).length;
  const awaitingSubagentCount = subagentStatuses.filter(
    (status) => status.activity === "awaiting",
  ).length;
  const recheckButton =
    rootSession != null &&
    isOwnerLevel(rootSession.permissionLevel) &&
    rootSession.labels?.[WRAPPER_LABEL_KEY] === "claude-code-native-ui" &&
    children.length > 0 ? (
      <ReconcileSubagentsButton
        key={rootSessionId}
        rootSessionId={rootSessionId}
        childIds={children.map((child) => child.id)}
      />
    ) : null;

  // Loading/error states only surface when there's no cached data to
  // show alongside the "main" row.
  if (isLoading && children.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <ViewModeToggle viewMode={viewMode} onViewModeChange={setViewMode} />
        <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      </div>
    );
  }
  if (error && children.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <ViewModeToggle viewMode={viewMode} onViewModeChange={setViewMode} />
        <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
          Failed to load agents.
        </div>
      </div>
    );
  }

  if (viewMode === "graph") {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
        <ViewModeToggle
          viewMode={viewMode}
          onViewModeChange={setViewMode}
          recheckButton={recheckButton}
        />
        <Suspense
          fallback={
            <div className="flex h-full flex-1 items-center justify-center text-sm text-muted-foreground">
              Loading graph…
            </div>
          }
        >
          <SubagentsGraphView conversationId={conversationId} rootSessionId={rootSessionId} />
        </Suspense>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <ViewModeToggle
        viewMode={viewMode}
        onViewModeChange={setViewMode}
        recheckButton={recheckButton}
      />
      <button
        type="button"
        data-testid="add-agent-button"
        onClick={() => setAddOpen(true)}
        className="hidden"
      >
        <PlusIcon className="size-3.5 shrink-0" />
        Add agent
      </button>
      <ul className="relative flex min-h-0 flex-1 flex-col gap-px overflow-y-auto px-2 pb-2 pt-2">
        <MainRow rootSessionId={rootSessionId} isActive={conversationId === rootSessionId} />
        <li
          data-testid="subagent-active-zone"
          className="flex items-center gap-2 border-b bg-muted/40 px-2.5 py-1 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase"
        >
          <span>Active · {sessionChildren.length}</span>
          {showsWarmCounts && (
            <span className="ml-auto font-normal normal-case">
              warm {warmCount} · cold {coldCount}
            </span>
          )}
        </li>
        {hostGroups.map((group) => (
          <HostGroupRows
            key={group.hostId}
            group={group}
            hostName={hostNameFor(group.hostId)}
            collapsed={collapsedHosts[group.hostId] ?? false}
            onToggleHost={() =>
              setCollapsedHosts((current) => ({
                ...current,
                [group.hostId]: !current[group.hostId],
              }))
            }
            collapsedCwds={collapsedCwds}
            onToggleCwd={(key) =>
              setCollapsedCwds((current) => ({ ...current, [key]: !current[key] }))
            }
            conversationId={conversationId}
            collapsedRows={collapsedRows}
            onToggleCollapsed={toggleCollapsedRow}
            canStopChildren={rootSession != null && isOwnerLevel(rootSession.permissionLevel)}
            onRequestStop={(id, label) => setStopTarget({ id, label })}
            hostNameFor={hostNameFor}
          />
        ))}
        {rootSubagents.length > 0 && (
          <>
            <li className="mt-1 border-t">
              <button
                type="button"
                data-testid="subagent-subagents-zone"
                aria-expanded={subagentsExpanded}
                onClick={() => setSubagentsExpanded((expanded) => !expanded)}
                className="flex w-full items-center gap-2 bg-muted/40 px-2.5 py-1 text-left text-[11px] font-semibold tracking-wide text-muted-foreground uppercase hover:bg-accent/60"
              >
                <span>Subagents · {rootSubagents.length}</span>
                <span className="ml-auto flex items-center gap-2 font-normal normal-case">
                  {runningSubagentCount > 0 && (
                    <span className="inline-flex items-center gap-1">
                      <RunningDot />
                      {runningSubagentCount} running
                    </span>
                  )}
                  {awaitingSubagentCount > 0 && (
                    <span className="rounded-full bg-warning/15 px-1.5 text-warning">
                      {awaitingSubagentCount} needs response
                    </span>
                  )}
                </span>
                <ChevronRightIcon
                  aria-hidden
                  className={cn("size-3.5 transition-transform", subagentsExpanded && "rotate-90")}
                />
              </button>
            </li>
            {subagentsExpanded &&
              rootSubagents.map((child, index) => (
                <SubagentRow
                  key={child.id}
                  child={child}
                  depth={1}
                  rootGuideContinues={index < rootSubagents.length - 1}
                  conversationId={conversationId}
                  collapsedRows={collapsedRows}
                  onToggleCollapsed={toggleCollapsedRow}
                  canStopChildren={rootSession != null && isOwnerLevel(rootSession.permissionLevel)}
                  onRequestStop={(id, label) => setStopTarget({ id, label })}
                  hostNameFor={hostNameFor}
                  showHostBar={false}
                />
              ))}
          </>
        )}
        <PastZoneHeader
          expanded={pastExpanded}
          count={past.children.length}
          hasMore={past.hasNextPage}
          onToggle={() => setPastExpanded((expanded) => !expanded)}
        />
        {pastExpanded && (
          <>
            {past.isLoading && past.children.length === 0 && (
              <li className="px-2.5 py-4 text-center text-sm text-muted-foreground">Loading…</li>
            )}
            {past.error && past.children.length === 0 && (
              <li className="px-2.5 py-4 text-center text-sm text-muted-foreground">
                Failed to load archived agents.
              </li>
            )}
            {past.children.map((child) => (
              <PastChildRow
                key={child.id}
                child={child}
                conversationId={conversationId}
                hostNameFor={hostNameFor}
              />
            ))}
            {past.hasNextPage && (
              <li>
                <button
                  type="button"
                  data-testid="subagent-past-load-more"
                  disabled={past.isFetchingNextPage}
                  onClick={past.fetchNextPage}
                  className="w-full px-2.5 py-1.5 text-left text-xs text-session-active hover:bg-accent/60 disabled:opacity-60"
                >
                  {past.isFetchingNextPage ? "Loading…" : "Load 20 more"}
                </button>
              </li>
            )}
          </>
        )}
      </ul>
      {/* Mounted only while open so a closed rail issues no /v1/agents
          fetch and carries none of the dialog's query dependencies. */}
      {addOpen && (
        <AddAgentDialog parentSessionId={rootSessionId} open={addOpen} onOpenChange={setAddOpen} />
      )}
      {stopTarget && <SubagentStopDialog target={stopTarget} onClose={() => setStopTarget(null)} />}
    </div>
  );
}

function HostGroupRows({
  group,
  hostName,
  collapsed,
  onToggleHost,
  collapsedCwds,
  onToggleCwd,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
  canStopChildren,
  onRequestStop,
  hostNameFor,
}: {
  group: ChildHostGroup;
  hostName: string;
  collapsed: boolean;
  onToggleHost: () => void;
  collapsedCwds: Record<string, boolean>;
  onToggleCwd: (key: string) => void;
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
  canStopChildren: boolean;
  onRequestStop: (id: string, label: string) => void;
  hostNameFor: (hostId: string | null | undefined) => string;
}) {
  const colorPreferences = useHostColorPreferences();
  const color = hostColor(group.hostId, hostName, colorPreferences);
  return (
    <>
      <li>
        <button
          type="button"
          data-testid="subagent-host-group"
          data-host-id={group.hostId}
          aria-expanded={!collapsed}
          onClick={onToggleHost}
          className="flex w-full items-center gap-1.5 px-2.5 py-1 text-left text-ui font-semibold hover:bg-accent/60"
        >
          {collapsed ? (
            <ChevronRightIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronDownIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
          )}
          <span
            aria-hidden
            data-testid="subagent-host-dot"
            className="host-color size-2 shrink-0 rounded-full"
            style={{ ...hostColorStyle(color), backgroundColor: "var(--host-color)" }}
          />
          <span className="truncate">{hostName}</span>
          <span className="shrink-0 font-normal text-muted-foreground">{group.count}</span>
        </button>
      </li>
      {!collapsed &&
        group.cwdGroups.map((cwdGroup) => {
          const key = cwdGroupKey(group.hostId, cwdGroup.cwd);
          const cwdCollapsed = collapsedCwds[key] ?? false;
          return (
            <Fragment key={key}>
              <li>
                <button
                  type="button"
                  data-testid="subagent-cwd-group"
                  aria-expanded={!cwdCollapsed}
                  title={cwdGroup.cwd ?? undefined}
                  onClick={() => onToggleCwd(key)}
                  className="flex w-full items-center gap-1.5 py-0.5 pr-2.5 pl-6 text-left font-mono text-[11.5px] font-medium text-muted-foreground hover:bg-accent/60"
                >
                  {cwdCollapsed ? (
                    <ChevronRightIcon aria-hidden className="size-3 shrink-0" />
                  ) : (
                    <ChevronDownIcon aria-hidden className="size-3 shrink-0" />
                  )}
                  <span className="truncate">
                    {cwdGroup.cwd ? shortenPath(cwdGroup.cwd) : UNKNOWN_DIRECTORY}
                  </span>
                </button>
              </li>
              {!cwdCollapsed &&
                cwdGroup.children.map((child, index) => (
                  <SubagentRow
                    key={child.id}
                    child={child}
                    depth={1}
                    rootGuideContinues={index < cwdGroup.children.length - 1}
                    conversationId={conversationId}
                    collapsedRows={collapsedRows}
                    onToggleCollapsed={onToggleCollapsed}
                    canStopChildren={canStopChildren}
                    onRequestStop={onRequestStop}
                    hostNameFor={hostNameFor}
                    showHostBar
                  />
                ))}
            </Fragment>
          );
        })}
    </>
  );
}

function cwdGroupKey(hostId: string, cwd: string | null): string {
  return `${hostId}\u0000${cwd ?? ""}`;
}

function PastZoneHeader({
  expanded,
  count,
  hasMore,
  onToggle,
}: {
  expanded: boolean;
  count: number;
  hasMore: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="mt-1 border-t">
      <button
        type="button"
        data-testid="subagent-past-zone"
        aria-expanded={expanded}
        onClick={onToggle}
        className="flex w-full items-center gap-2 bg-muted/40 px-2.5 py-1 text-left text-[11px] font-semibold tracking-wide text-muted-foreground uppercase hover:bg-accent/60"
      >
        <span>
          Past
          {count > 0 ? ` · ${count}${hasMore ? "+" : ""}` : ""}
        </span>
        <ChevronRightIcon
          aria-hidden
          className={cn("ml-auto size-3.5 transition-transform", expanded && "rotate-90")}
        />
      </button>
    </li>
  );
}

function SubagentStopDialog({ target, onClose }: { target: StopTarget; onClose: () => void }) {
  const stopSession = useStopSession();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !stopSession.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Stop sub-agent?</DialogTitle>
          <DialogDescription>
            This stops <span className="font-medium">{target.label}</span>. Its conversation and
            history are kept.
          </DialogDescription>
        </DialogHeader>
        {stopSession.isError && (
          <p className="text-ui text-destructive" role="alert">
            Couldn't stop the sub-agent
            {stopSession.error instanceof Error && stopSession.error.message
              ? `: ${stopSession.error.message}`
              : " — it may still be running"}
            . Try again in a moment.
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose} disabled={stopSession.isPending}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            data-testid="stop-subagent-confirm"
            onClick={() => stopSession.mutate(target.id, { onSuccess: onClose })}
            loading={stopSession.isPending}
            componentId="subagents.stop"
          >
            Stop sub-agent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ViewModeToggle({
  viewMode,
  onViewModeChange,
  recheckButton,
}: {
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  recheckButton?: ReactNode;
}) {
  return (
    <div className="flex h-11 shrink-0 items-center gap-0.5 border-b px-2">
      <h2 className="pl-1 font-medium text-ui">Agents</h2>
      <div className="ml-auto flex items-center gap-0.5">
        {recheckButton}
        <Button
          variant={viewMode === "list" ? "secondary" : "ghost"}
          size="icon-xs"
          onClick={() => onViewModeChange("list")}
          aria-label="List view"
          title="List view"
          data-testid="view-mode-list"
        >
          <ListIcon className="size-3.5" />
        </Button>
        <Button
          variant={viewMode === "graph" ? "secondary" : "ghost"}
          size="icon-xs"
          onClick={() => onViewModeChange("graph")}
          aria-label="Graph view"
          title="Graph view"
          data-testid="view-mode-graph"
        >
          <NetworkIcon className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

// Settled states are de-emphasized (dimmed) so live agents dominate the list.
// Kept separate from QUIET_STATE: ``working`` is quiet (no label word) but must
// NOT be dimmed — an actively-working agent should stay full-strength.
const SETTLED_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: false,
  awaiting: false,
  failed: false,
  // Not dimmed — a disconnected runner is something the user may want to
  // notice and act on (retry/reconnect), so it stays full-strength.
  disconnected: false,
  unverified: false,
  other: false,
  done: true,
  idle: true,
};

/**
 * Resolve the semantic role that chooses a sub-agent's category icon.
 *
 * Native runtimes may give an instance a friendly nickname (for example,
 * Codex's "Archimedes") while carrying its functional role separately. The
 * nickname remains the row label; the role drives iconography.
 */
function iconRoleForChild(child: ChildSessionInfo): string | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  if (wrapper === CODEX_NATIVE_SUBAGENT_WRAPPER) {
    return child.labels?.[CODEX_NATIVE_SUBAGENT_ROLE_LABEL] ?? child.tool;
  }
  if (wrapper === ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER) {
    return child.labels?.[ANTIGRAVITY_NATIVE_SUBAGENT_ROLE_LABEL] ?? child.tool;
  }
  return child.tool;
}

const STATUS_AVATAR_CLASS: Record<AgentActivity, string> = {
  launching: "bg-muted text-muted-foreground",
  working: "bg-muted text-muted-foreground",
  awaiting: "bg-warning/15 text-warning",
  failed: "bg-destructive/10 text-destructive",
  disconnected: "bg-muted text-muted-foreground",
  unverified: "bg-muted text-muted-foreground",
  other: "bg-muted text-muted-foreground",
  done: "bg-success/10 text-success",
  idle: "bg-muted text-muted-foreground",
};

const STATUS_AVATAR_ICON: Record<AgentActivity, AgentRowIcon | null> = {
  launching: null,
  working: null,
  awaiting: CircleHelpIcon,
  failed: CircleAlertIcon,
  disconnected: UnplugIcon,
  unverified: CircleHelpIcon,
  other: EllipsisIcon,
  done: CheckIcon,
  idle: PauseIcon,
};

/** Compact state tile shown before each agent avatar. */
function StatusAvatar({ activity, label, details }: AgentStatus) {
  const title = details ? `${label}: ${details}` : label;
  const Icon = STATUS_AVATAR_ICON[activity];
  return (
    <span
      aria-label={title}
      title={title}
      data-testid="subagent-status-avatar"
      data-activity={activity}
      className={cn(
        "flex size-6 shrink-0 items-center justify-center rounded-md",
        STATUS_AVATAR_CLASS[activity],
      )}
    >
      {Icon ? (
        <Icon aria-hidden="true" className="size-3.5" />
      ) : (
        <RunningDot className="size-3.5 text-current" />
      )}
    </span>
  );
}

function WarmStatePill({ state }: { state: ChildSessionInfo["warm_state"] }) {
  if (state !== "warm" && state !== "cold") return null;
  const warm = state === "warm";
  return (
    <span
      data-testid="subagent-warm-state"
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full border border-transparent px-1.5 text-[10px] leading-4",
        warm ? "bg-warning/15 text-warning" : "bg-session-active/15 text-session-active",
      )}
    >
      <span className={cn("size-1.5 rounded-full", warm ? "bg-warning" : "bg-session-active")} />
      {warm ? "Warm" : "Cold"}
    </span>
  );
}

/**
 * Tooltip body shared by active and past rows: agent · host, the full cwd,
 * the branch when the session recorded one, and the task summary.
 */
function ChildTooltipContent({ child, hostName }: { child: ChildSessionInfo; hostName: string }) {
  const display = childAgentDisplay(child);
  return (
    <div className="flex flex-col gap-0.5 text-left">
      <span>{[display, hostName].filter(Boolean).join(" · ")}</span>
      {child.cwd && <span className="break-all font-mono text-[11px]">{child.cwd}</span>}
      {child.git_branch && <span>branch {child.git_branch}</span>}
      {child.task_summary && <span>{child.task_summary}</span>}
    </div>
  );
}

/**
 * First row of the Subagents list — a navigation link back to the
 * parent (root) session. Always present, even when the parent has
 * no children, so the rail is a complete navigation surface for the
 * parent-children tree.
 *
 * The leading icon doubles as the agent-kind indicator (a brand glyph for
 * native wrappers/harnesses, the generic bot otherwise). Sub-agent rows nest
 * below with their own icons, so the "main vs sub-agent" distinction is
 * carried by position and the indentation gutter rather than a pill.
 */
// Cap matches the server's child-session preview so the main row reads
// consistently with the child rows (CSS truncates to one line regardless;
// this just keeps the DOM string bounded).
const MAIN_PREVIEW_MAX_CHARS = 150;

/**
 * Derive a one-line preview of the root session's most recent message from
 * its snapshot items, mirroring the server's child-session preview so the
 * "main" row reads like the child rows below it.
 *
 * Scans newest-first for the last ``message`` item and joins its text
 * content blocks (assistant ``output_text`` / user ``input_text``).
 *
 * @param items - The root session's snapshot items (oldest-first), or
 *   ``undefined`` while the snapshot is still loading.
 * @returns The latest message text, trimmed and length-capped, or ``null``
 *   when the session has no message item yet.
 */
function mainMessagePreview(items: SessionItem[] | undefined): string | null {
  if (!items) return null;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type !== "message") continue;
    const content = (item as { data?: { content?: unknown } }).data?.content;
    if (!Array.isArray(content)) continue;
    const text = content
      .map((block) =>
        block && typeof block === "object" && "text" in block
          ? String((block as { text: unknown }).text)
          : "",
      )
      .join("")
      .trim();
    if (text) {
      return text.length > MAIN_PREVIEW_MAX_CHARS
        ? `${text.slice(0, MAIN_PREVIEW_MAX_CHARS)}…`
        : text;
    }
  }
  return null;
}

function MainRow({ rootSessionId, isActive }: { rootSessionId: string; isActive: boolean }) {
  const { session } = useSession(rootSessionId);
  const search = sessionNavigationSearch(useLocation().search);
  const child = sessionLike(session, rootSessionId);
  // Same wrapper-label probe used by the sidebar (Sidebar.tsx) and
  // TerminalFirstContext to decide a session is claude/codex-native.
  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  const isNessie = session?.agentName === "nessie";
  // Native wrappers show the product name (mirroring the sidebar) instead
  // of the spec's YAML name (e.g. "claude-native-ui"); other agents show
  // their agent name, with "main" only while the session loads or when it
  // carries no name.
  const label = nativeAgent?.displayName ?? session?.agentName ?? "main";
  const preview = mainMessagePreview(session?.items);
  return (
    <li className="relative">
      <span
        aria-hidden="true"
        style={{ left: ROOT_GUIDE_CENTER_PX }}
        className="pointer-events-none absolute bottom-0 top-10 z-10 border-l border-dashed border-border/70"
      />
      <Link
        // Drop session-scoped params (``file``, ``diff``, ``comment``,
        // ``view``, ``message``) when navigating in the rail — those are tied to
        // one session's file-viewer state and must not bleed into the
        // next. Global params like ``?debug=1`` are preserved by
        // ``sessionNavigationSearch`` so debug mode stays on across navigation.
        to={{ pathname: `/c/${rootSessionId}`, search }}
        data-testid="subagent-main-row"
        data-root-session-id={rootSessionId}
        data-agent-kind={
          nativeAgent != null ? `${nativeAgent.key}-native` : isNessie ? "nessie" : "agent"
        }
        className={cn(
          "flex w-full flex-col gap-0.5 rounded-md px-2.5 py-2 text-left",
          isActive ? "bg-accent" : "hover:bg-muted",
        )}
      >
        <div className="flex w-full items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-[2px]">
              <span
                data-testid="subagent-main-harness-icon"
                className="flex size-8 shrink-0 items-center justify-center rounded-xl border border-border bg-background"
              >
                <ComposerAgentIcon
                  agent={{
                    name: session?.agentName ?? nativeAgent?.agentName ?? "",
                    harness: session?.harness ?? nativeAgent?.harness ?? null,
                  }}
                  className="size-5"
                />
              </span>
              <RailAgentBadge child={child} />
              <span className="min-w-0 truncate text-base font-semibold">{label}</span>
            </div>
            {preview && (
              <p
                data-testid="subagent-main-preview"
                className="truncate text-sm text-muted-foreground"
              >
                {preview}
              </p>
            )}
          </div>
        </div>

      </Link>
    </li>
  );
}

const ROOT_GUIDE_CENTER_PX = 26;

// The root harness avatar's center sits at 26px (10px row inset + 16px).
// Center direct children on that guide. Align the nested connector container
// with its parent's title container, then preserve that step at deeper levels.
const ROW_BASE_PADDING_PX = 14;
const ROW_DEPTH_STEP_PX = 90;
const SUBAGENT_ROW_OFFSET_PX = 26;
const ROW_VERTICAL_PADDING_PX = 4;
const ROW_CONNECTOR_SIZE_PX = 24;

function rowPaddingLeft(depth: number): number {
  return (
    ROW_BASE_PADDING_PX + (depth - 1) * ROW_DEPTH_STEP_PX - (depth > 1 ? SUBAGENT_ROW_OFFSET_PX : 0)
  );
}

function SubagentRow({
  child,
  depth,
  rootGuideContinues,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
  canStopChildren,
  onRequestStop,
  hostNameFor,
  showHostBar,
}: {
  child: ChildSessionInfo;
  /** Levels below the root, 1 = direct child of "main". */
  depth: number;
  /** Whether the root guide must continue to a later first-level sibling. */
  rootGuideContinues: boolean;
  /** The conversation currently rendered in main, for row highlighting. */
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
  canStopChildren: boolean;
  onRequestStop: (id: string, label: string) => void;
  hostNameFor: (hostId: string | null | undefined) => string;
  /** Whether this row may draw the host-colour bar (ACTIVE zone, real rows). */
  showHostBar: boolean;
}) {
  const collapsed = collapsedRows[child.id] ?? false;
  const status = childStatus(child);
  const search = sessionNavigationSearch(useLocation().search);
  const mirror = isHarnessSubagent(child);
  const showBadge = useChildAgentBadge(child) != null;
  const harnessAgent = harnessAgentForChild(child);
  const RoleIcon = iconForAgentType(iconRoleForChild(child));
  const primary = childPrimaryLabel(child);
  const hostName = hostNameFor(child.host_id);
  const colorPreferences = useHostColorPreferences();
  const hostEntry = hostColor(child.host_id, hostName, colorPreferences);
  const isActive = conversationId === child.id;
  // De-emphasize settled rows (done/idle) so working/failed agents dominate
  // — but never the row the user is currently viewing.
  const dim = !isActive && SETTLED_STATE[status.activity];
  // This child's own sub-agents, rendered as the next tree level.
  // Disabled (null id) at the depth cap so the fan-out of fetches is
  // bounded; ``useChildSessions`` skips the query entirely for null.
  const { children: grandchildren } = useChildSessions(depth < MAX_TREE_DEPTH ? child.id : null);
  const hasGrandchildren = grandchildren.length > 0;
  const ToggleIcon = collapsed ? ChevronRightIcon : ChevronDownIcon;
  const canStop =
    canStopChildren &&
    (status.activity === "launching" ||
      status.activity === "working" ||
      status.activity === "awaiting" ||
      (status.activity === "unverified" && child.busy));
  const secondary = child.last_message_preview ?? child.task_summary;
  const hostBar = showHostBar && !mirror;
  return (
    <>
      <li className="group/agent relative">
        {depth === 1 ? (
          <>
            <span
              aria-hidden="true"
              data-root-guide-segment="upper"
              style={{ left: ROOT_GUIDE_CENTER_PX, height: ROW_VERTICAL_PADDING_PX }}
              className="pointer-events-none absolute top-0 border-l border-dashed border-border/70"
            />
            {rootGuideContinues && (
              <span
                aria-hidden="true"
                data-root-guide-segment="lower"
                style={{
                  left: ROOT_GUIDE_CENTER_PX,
                  top: ROW_VERTICAL_PADDING_PX + ROW_CONNECTOR_SIZE_PX,
                }}
                className="pointer-events-none absolute bottom-0 border-l border-dashed border-border/70"
              />
            )}
          </>
        ) : rootGuideContinues ? (
          <span
            aria-hidden="true"
            data-root-guide-segment="continuation"
            style={{ left: ROOT_GUIDE_CENTER_PX }}
            className="pointer-events-none absolute inset-y-0 border-l border-dashed border-border/70"
          />
        ) : null}
        {hasGrandchildren && (
          <button
            type="button"
            data-testid="subagent-collapse-toggle"
            aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand subagents" : "Collapse subagents"}
            style={{ left: rowPaddingLeft(depth) }}
            className="absolute top-1 z-10 flex size-6 items-center justify-center rounded-md bg-transparent text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            onClick={(event) => {
              event.stopPropagation();
              onToggleCollapsed(child.id);
            }}
          >
            <ToggleIcon aria-hidden="true" className="size-3.5" />
          </button>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <Link
              // See MainRow: drop session-scoped params on rail navigation
              // (preserving global ones like ``?debug=1``) so a sticky
              // ``?file=`` from the previous session doesn't carry over.
              to={{ pathname: `/c/${child.id}`, search }}
              data-testid="subagent-row"
              data-child-session-id={child.id}
              data-depth={depth}
              style={{
                paddingLeft: rowPaddingLeft(depth),
                ...(hostBar
                  ? { ...hostColorStyle(hostEntry), boxShadow: "inset 3px 0 0 var(--host-color)" }
                  : {}),
              }}
              className={cn(
                "flex w-full flex-col gap-0.5 rounded-md py-1 text-left",
                hostBar && "host-color",
                canStop ? "pr-12" : "pr-1",
                isActive ? "bg-accent" : "group-hover/agent:bg-muted",
                dim && "opacity-60 group-hover/agent:opacity-100",
              )}
            >
              <div className="flex w-full items-start gap-2">
                {hasGrandchildren ? (
                  <span aria-hidden="true" className="size-6 shrink-0" />
                ) : (
                  <span
                    aria-hidden="true"
                    className="flex size-6 shrink-0 items-center justify-center rounded-md bg-transparent"
                  >
                    {depth === 1 ? (
                      <CircleDotIcon
                        data-subagent-connector
                        className="size-4 text-muted-foreground/60"
                      />
                    ) : (
                      <CornerDownRightIcon
                        // Decorative nesting connector — the role icon beside it carries
                        // the meaning, so hide this from the accessibility tree.
                        data-subagent-connector
                        className="size-4 text-muted-foreground/60"
                      />
                    )}
                  </span>
                )}
                <StatusAvatar {...status} />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-[2px]">
                    <span
                      data-testid="subagent-agent-avatar"
                      className="flex size-6 shrink-0 items-center justify-center"
                    >
                      {harnessAgent ? (
                        <ComposerAgentIcon agent={harnessAgent} className="size-4" />
                      ) : (
                        <RoleIcon className="size-4 text-muted-foreground" />
                      )}
                    </span>
                    {showBadge && <RailAgentBadge child={child} />}
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">{primary}</span>
                    {child.routed_model ? (
                      // Model the intelligent router picked for this sub-agent — the
                      // per-subagent half of routing visibility.
                      <span
                        data-testid="subagent-routed-model"
                        title={`Smart routing picked ${child.routed_model}`}
                        className="shrink-0 truncate font-mono text-[10px] text-muted-foreground"
                      >
                        {shortModelName(child.routed_model)}
                      </span>
                    ) : null}
                    {!mirror && <WarmStatePill state={child.warm_state} />}
                  </div>
                  {secondary && (
                    <p className="mt-0.5 truncate text-sm text-muted-foreground">{secondary}</p>
                  )}
                </div>
              </div>
            </Link>
          </TooltipTrigger>
          <TooltipContent side="left" align="center">
            <ChildTooltipContent child={child} hostName={hostName} />
          </TooltipContent>
        </Tooltip>
        {canStop && (
          <Button
            type="button"
            variant="destructive"
            size="icon"
            data-testid="stop-subagent"
            aria-label={`Stop sub-agent ${primary}`}
            title={`Stop sub-agent ${primary}`}
            className="absolute top-1/2 right-1 z-10 -translate-y-1/2"
            onClick={() => onRequestStop(child.id, primary)}
          >
            <CircleStopIcon className="size-4" />
          </Button>
        )}
      </li>
      {!collapsed &&
        grandchildren.map((grandchild) => (
          <SubagentRow
            key={grandchild.id}
            child={grandchild}
            depth={depth + 1}
            rootGuideContinues={rootGuideContinues}
            conversationId={conversationId}
            collapsedRows={collapsedRows}
            onToggleCollapsed={onToggleCollapsed}
            canStopChildren={canStopChildren}
            onRequestStop={onRequestStop}
            hostNameFor={hostNameFor}
            showHostBar={showHostBar}
          />
        ))}
    </>
  );
}

/**
 * Archived time as "Today HH:MM" / "Yesterday HH:MM" / a short date.
 * ``archived_at`` is epoch seconds.
 */
function formatArchivedTime(archivedAt: number | null | undefined): string | null {
  if (archivedAt == null) return null;
  const date = new Date(archivedAt * 1000);
  if (Number.isNaN(date.getTime())) return null;
  const startOfDay = (value: Date) =>
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const daysAgo = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86_400_000);
  const clock = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  if (daysAgo <= 0) return `Today ${clock}`;
  if (daysAgo === 1) return `Yesterday ${clock}`;
  return date.toLocaleDateString(undefined, {
    ...(date.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
  });
}

function PastChildRow({
  child,
  conversationId,
  hostNameFor,
}: {
  child: ChildSessionInfo;
  conversationId: string;
  hostNameFor: (hostId: string | null | undefined) => string;
}) {
  const search = sessionNavigationSearch(useLocation().search);
  const harnessAgent = harnessAgentForChild(child);
  const RoleIcon = iconForAgentType(iconRoleForChild(child));
  const primary = childPrimaryLabel(child);
  const showBadge = useChildAgentBadge(child) != null;
  const hostName = hostNameFor(child.host_id);
  const archivedLabel = formatArchivedTime(child.archived_at);
  const isActive = conversationId === child.id;
  const cwdLabel = child.cwd ? shortenPath(child.cwd) : UNKNOWN_DIRECTORY;
  return (
    <li>
      <Tooltip>
        <TooltipTrigger asChild>
          <Link
            to={{ pathname: `/c/${child.id}`, search }}
            data-testid="subagent-past-row"
            data-child-session-id={child.id}
            className={cn(
              "flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60",
              isActive && "bg-accent",
            )}
          >
            <div className="flex w-full items-center gap-1">
              {harnessAgent ? (
                <ComposerAgentIcon agent={harnessAgent} className="size-3.5" />
              ) : (
                <RoleIcon className="size-3.5 shrink-0 text-muted-foreground" />
              )}
              <RailAgentBadge child={child} />
              <span className="shrink-0 truncate text-sm font-medium">{primary}</span>
              <span className="flex-1" />
              {archivedLabel && (
                <span className="shrink-0 text-[11px] text-muted-foreground">{archivedLabel}</span>
              )}
            </div>
            {/* Aligned with the title: 14px icon + 4px gap, plus the 20px badge + 4px gap when configured. */}
            <p
              className={cn(
                "truncate text-xs text-muted-foreground",
                showBadge ? "pl-[42px]" : "pl-[18px]",
              )}
            >
              {hostName} · {cwdLabel}
            </p>
          </Link>
        </TooltipTrigger>
        <TooltipContent side="left" align="center">
          <ChildTooltipContent child={child} hostName={hostName} />
        </TooltipContent>
      </Tooltip>
    </li>
  );
}
