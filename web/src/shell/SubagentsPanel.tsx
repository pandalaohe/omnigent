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
import type { ReactNode } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  CircleStopIcon,
  ListIcon,
  NetworkIcon,
  PlusIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { RailAgentBadge, childAgentDisplay } from "@/components/RailAgentBadge";
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
import { hostColor, hostDisplayName } from "@/lib/hostColors";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
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
import {
  activityDotClassName,
  childStatus,
  sessionStatus,
  type AgentActivity,
  type AgentStatus,
} from "./subagentStatus";
import { AddAgentDialog } from "./AddAgentDialog";
import { ReconcileSubagentsButton } from "./ReconcileSubagentsButton";

const SubagentsGraphView = lazy(() =>
  import("./SubagentsGraphView").then((m) => ({ default: m.SubagentsGraphView })),
);

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
    harness: session?.harness,
    host_id: session?.hostId,
    labels: session?.labels ?? {},
  };
}

function hostNameForHost(hostId: string | null | undefined, hosts: Map<string, Host>): string {
  return hostDisplayName(hostId, hostId ? hosts.get(hostId) : undefined);
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
  const hostGroups = useMemo(
    () => groupChildren(children, rootSession?.hostId ?? null),
    [children, rootSession?.hostId],
  );
  const warmCount = children.filter((child) => child.warm_state === "warm").length;
  const coldCount = children.filter((child) => child.warm_state === "cold").length;
  const showsWarmCounts = children.some((child) => child.warm_state != null);
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
      <ul className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1">
        <MainRow
          rootSessionId={rootSessionId}
          isActive={conversationId === rootSessionId}
          hostNameFor={hostNameFor}
        />
        <li
          data-testid="subagent-active-zone"
          className="flex items-center gap-2 border-b bg-muted/40 px-2.5 py-1 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase"
        >
          <span>Active · {children.length}</span>
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
  const dotColor = hostColor(group.hostId, hostName, colorPreferences).hex;
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
            className="size-2 shrink-0 rounded-full"
            style={{ backgroundColor: dotColor }}
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
                cwdGroup.children.map((child) => (
                  <SubagentRow
                    key={child.id}
                    child={child}
                    depth={1}
                    conversationId={conversationId}
                    collapsedRows={collapsedRows}
                    onToggleCollapsed={onToggleCollapsed}
                    canStopChildren={canStopChildren}
                    onRequestStop={onRequestStop}
                    hostNameFor={hostNameFor}
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
      <h2 className="font-medium text-ui">Agents</h2>
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

// Quiet states show only an indicator — the word lives in the tooltip — so the
// row stays clean. Working is quiet too: the pulsing pink dot already reads as
// "active", so the redundant "Working" label is dropped. The eye still lands on
// agents that need input or are in trouble, which keep their word.
const QUIET_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: true,
  awaiting: false,
  failed: false,
  // Quiet — show only the grey dot (the word lives in the tooltip), like the
  // idle/done/working dot states. The colored dot is enough to flag the
  // liveness loss without adding label text to the row.
  disconnected: true,
  unverified: false,
  other: false,
  done: true,
  idle: true,
};

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
 * Indicator + optional label shared by the main and child rows. The working
 * state reuses the sidebar's RunningDot in the same grey tone, so
 * "active" reads identically across the app; other states are a single
 * tokenized dot.
 *
 * The indicator is rendered last (label first) so that, with the indicator
 * right-aligned in the row, every row's dot lands in the same column
 * regardless of label width or whether the label is shown — otherwise a
 * wide label like "Failed" pushes its dot left of a bare "Idle" dot.
 *
 * @param status - The resolved activity + label to render.
 */
function StatusIndicator({ activity, label, details }: AgentStatus) {
  const title = details ? `${label}: ${details}` : label;
  // Awaiting renders the exact same "Needs response" tag as the sidebar
  // (SessionStateBadge) so the approval affordance reads identically across
  // the app. The tag carries its own copy, so the row's separate label word
  // is omitted to avoid duplicating the text.
  if (activity === "awaiting") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center text-sm"
      >
        <Badge className="border-transparent bg-warning/15 text-warning">Needs response</Badge>
      </span>
    );
  }
  if (activity === "failed") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center gap-1 text-destructive text-sm"
      >
        <span>{label}</span>
        <span
          className={cn(
            "inline-block size-2 shrink-0 rounded-full",
            activityDotClassName("failed"),
          )}
        />
      </span>
    );
  }
  // ``disconnected`` falls through to the quiet default below: it's a
  // QUIET_STATE, so only the grey --muted-foreground dot renders (no inline
  // word) — the cause stays in the tooltip / aria-label. Distinct from the
  // red "Failed" pill above, without repurposing the shared amber --warning.
  //
  // Launching's inline word reads in the blue --session-active hue to match
  // its dot; every other state here keeps the neutral muted text — the verbatim
  // "other" word stays grey, and idle/done/disconnected show no word at all.
  const wrapperTextClass =
    activity === "launching" ? "text-session-active" : "text-muted-foreground";
  return (
    <span
      aria-label={title}
      title={title}
      data-testid="subagent-status-dot"
      className={cn("inline-flex shrink-0 items-center gap-1 text-sm", wrapperTextClass)}
    >
      {!QUIET_STATE[activity] && <span>{label}</span>}
      {activity === "working" ? (
        <RunningDot />
      ) : (
        <span
          className={cn(
            "inline-block size-2 shrink-0 rounded-full",
            activityDotClassName(activity),
          )}
        />
      )}
    </span>
  );
}

function WarmStatePill({ state }: { state: ChildSessionInfo["warm_state"] }) {
  if (state !== "warm" && state !== "cold") return null;
  return (
    <span
      data-testid="subagent-warm-state"
      className="shrink-0 rounded-full border border-border px-1.5 text-[10px] leading-4 text-muted-foreground"
    >
      {state === "warm" ? "Warm" : "Cold"}
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
 * The badge doubles as the agent-kind indicator (letters = agent,
 * border = host); sub-agent rows nest below with their own badges, so
 * the "main vs sub-agent" distinction is carried by position and the
 * indentation gutter rather than a pill.
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

function MainRow({
  rootSessionId,
  isActive,
  hostNameFor,
}: {
  rootSessionId: string;
  isActive: boolean;
  hostNameFor: (hostId: string | null | undefined) => string;
}) {
  const { session } = useSession(rootSessionId);
  const search = sessionNavigationSearch(useLocation().search);
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
    <li>
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
          "flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60",
          isActive && "bg-accent",
        )}
      >
        <div className="flex w-full items-center gap-1">
          <RailAgentBadge
            child={sessionLike(session, rootSessionId)}
            hostName={hostNameFor(session?.hostId)}
          />
          <span className="shrink-0 truncate text-sm font-medium">{label}</span>
          <span className="flex-1" />
          <StatusIndicator {...sessionStatus(session?.status, session?.lastTaskError)} />
        </div>
        {preview && (
          // Indented to align with the title text above: 22px badge + 4px gap.
          <p
            data-testid="subagent-main-preview"
            className="truncate pl-[26px] text-sm text-muted-foreground"
          >
            {preview}
          </p>
        )}
      </Link>
    </li>
  );
}

// Indentation: depth 1 keeps the original 24px gutter (pl-6); each
// further level steps in by another 14px so the nesting reads as a tree.
const ROW_BASE_PADDING_PX = 24;
const ROW_DEPTH_STEP_PX = 14;
const ROW_TOGGLE_SIZE_PX = 16;

function rowPaddingLeft(depth: number): number {
  return ROW_BASE_PADDING_PX + (depth - 1) * ROW_DEPTH_STEP_PX;
}

function SubagentRow({
  child,
  depth,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
  canStopChildren,
  onRequestStop,
  hostNameFor,
}: {
  child: ChildSessionInfo;
  /** Levels below the root, 1 = direct child of "main". */
  depth: number;
  /** The conversation currently rendered in main, for row highlighting. */
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
  canStopChildren: boolean;
  onRequestStop: (id: string, label: string) => void;
  hostNameFor: (hostId: string | null | undefined) => string;
}) {
  const collapsed = collapsedRows[child.id] ?? false;
  const status = childStatus(child);
  const search = sessionNavigationSearch(useLocation().search);
  const primary = childPrimaryLabel(child);
  const hostName = hostNameFor(child.host_id);
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
  return (
    <>
      <li className="relative">
        {hasGrandchildren && (
          <button
            type="button"
            data-testid="subagent-collapse-toggle"
            aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand subagents" : "Collapse subagents"}
            style={{ left: rowPaddingLeft(depth) - ROW_TOGGLE_SIZE_PX }}
            className="absolute top-2 z-10 flex size-4 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
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
              // Left gutter (depth-stepped) nests this row under its parent.
              style={{ paddingLeft: rowPaddingLeft(depth) }}
              className={cn(
                "flex w-full flex-col gap-0.5 py-2 text-left hover:bg-accent/60",
                canStop ? "pr-12" : "pr-2.5",
                isActive && "bg-accent",
                dim && "opacity-60 hover:opacity-100",
              )}
            >
              <div className="flex w-full items-center gap-1">
                <RailAgentBadge child={child} hostName={hostName} />
                <span className="shrink-0 truncate text-sm font-medium">{primary}</span>
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
                <span className="flex-1" />
                <WarmStatePill state={child.warm_state} />
                <StatusIndicator {...status} />
              </div>
              {secondary && (
                // Preview indented to align with the title text above:
                // 22px badge + 4px gap.
                <p className="truncate pl-[26px] text-sm text-muted-foreground">{secondary}</p>
              )}
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
            conversationId={conversationId}
            collapsedRows={collapsedRows}
            onToggleCollapsed={onToggleCollapsed}
            canStopChildren={canStopChildren}
            onRequestStop={onRequestStop}
            hostNameFor={hostNameFor}
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
  const primary = childPrimaryLabel(child);
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
              <RailAgentBadge child={child} hostName={hostName} />
              <span className="shrink-0 truncate text-sm font-medium">{primary}</span>
              <span className="flex-1" />
              {archivedLabel && (
                <span className="shrink-0 text-[11px] text-muted-foreground">{archivedLabel}</span>
              )}
            </div>
            <p className="truncate pl-[26px] text-xs text-muted-foreground">
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
