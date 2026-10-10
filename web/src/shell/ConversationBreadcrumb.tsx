import { BotIcon, ChevronLeftIcon } from "lucide-react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { Link } from "@/lib/routing";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { RailAgentBadge, childAgentDisplay } from "@/components/RailAgentBadge";
import { WorktreeStatusMark } from "@/components/WorktreeStatusMark";
import { useHosts } from "@/hooks/useHosts";
import { copyText } from "@/lib/clipboard";
import { hostDisplayName } from "@/lib/hostColors";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForSubagentWrapper,
} from "@/lib/nativeCodingAgents";
import type { Agent } from "@/hooks/useAgents";
import { cn } from "@/lib/utils";
import { childPrimaryLabel, shortenPath, type ChildSessionLike } from "./subagentRailGroups";
import { ProjectRowIcon } from "./ProjectPicker";

/**
 * `agent @ host · cwd` chip for a child session's header. Hover shows the
 * full path; clicking copies it.
 */
function ChildPlacementChip({
  display,
  hostName,
  cwd,
}: {
  display: string;
  hostName: string;
  cwd: string | null;
}) {
  const placement = `${display} @ ${hostName}`;
  const label = cwd ? `${placement} · ${shortenPath(cwd)}` : placement;
  const copy = () => {
    if (!cwd) return;
    void copyText(cwd).then(
      () => toast.success("Copied working directory.", { duration: 1500 }),
      () => toast.error("Couldn't copy the working directory."),
    );
  };
  return (
    <button
      type="button"
      data-testid="breadcrumb-child-placement"
      title={cwd ?? undefined}
      onClick={copy}
      className={cn(
        "min-w-0 shrink truncate rounded-[5px] border border-border px-1.5 py-px font-mono text-[12px] text-muted-foreground",
        "hover:text-foreground max-md:basis-full max-md:text-left md:ml-auto",
      )}
    >
      {label}
    </button>
  );
}

/**
 * `[folder] / <title> [/ <badge> <child name> <agent @ host · cwd>]` breadcrumb
 * for the active conversation.
 *
 * Rendered in the chat header's left slot (ChatHeader). On the macOS shell with
 * the sidebar collapsed the slot is padded clear of the traffic lights and the
 * title-bar cluster (see `.traffic-light-clearance` in index.css), so the
 * breadcrumb stays in the header — truncating within its flex — rather than
 * overlapping the window controls.
 *
 * The caller mounts this when there is a title or a parent route to climb
 * back to. Segments self-gate: the folder only shows when filed, the child
 * segment only inside a child. The title links back to the parent when
 * `titleLinkTo` is set (viewing a sub-agent), else it's plain text. A child
 * whose snapshot is known shows its own badge, label and placement chip
 * (agent @ host · cwd), matching the Agents rail; without the snapshot the
 * generic sub-agent identity stands in while it loads.
 */
export function ConversationBreadcrumb({
  sessionId,
  conversationTitle,
  projectName,
  projectIcon,
  projectTag,
  titleSlot,
  titleLinkTo,
  isChildSession,
  subAgentName,
  boundAgent,
  wrapperLabel,
  childSession,
  childCwd,
  actions,
  className,
}: {
  sessionId?: string | null;
  /** The conversation's display name. */
  conversationTitle: string;
  /** Project the conversation is filed under, or `null` when unfiled. */
  projectName: string | null;
  /**
   * The filed project's chosen emoji icon (a unicode grapheme), or `null` for
   * the default folder glyph. Used by the static leading segment (when
   * `projectTag` is omitted); the interactive tag carries its own icon.
   */
  projectIcon?: string | null;
  /**
   * Leading folder segment. When set (the desktop title shortcut), it replaces
   * the static folder icon with an interactive "Move to…" trigger and self-gates
   * its own visibility. When omitted, a static folder renders iff `projectName`
   * is set — the fallback for surfaces without the shortcut (e.g. sub-agents).
   */
  projectTag?: ReactNode;
  /**
   * Interactive title node (the desktop click-to-rename control). Replaces the
   * plain-text title. Ignored when `titleLinkTo` is set — a sub-agent keeps its
   * back-to-parent link rather than becoming editable.
   */
  titleSlot?: ReactNode;
  /** Parent-session route the title links to, or `undefined` for plain text. */
  titleLinkTo?: string;
  /** Whether the active session is a sub-agent (appends its identity). */
  isChildSession: boolean;
  /**
   * The session's own `sub_agent_name` — the dispatched sub-agent's identity
   * (e.g. a `sys_session_send` child of a bundle). Preferred over
   * `boundAgent.name`, which for such children is the *parent* bundle's row.
   */
  subAgentName?: string | null;
  /** The bound agent — names the sub-agent segment. */
  boundAgent: Agent | undefined;
  /** The session's `omnigent.wrapper` label — names a native sub-agent's vendor. */
  wrapperLabel: string | null;
  /**
   * The active child's snapshot, normalized to the shared child shape. When
   * present, the segment after the parent link renders the child's own badge,
   * label and placement chip instead of the generic sub-agent identity.
   */
  childSession?: ChildSessionLike | null;
  /** Effective cwd from the child's snapshot (server-computed), for the chip. */
  childCwd?: string | null;
  /** Session-management menu rendered immediately after the title. */
  actions?: ReactNode;
  /** Extra classes for the context (header vs title-bar strip). */
  className?: string;
}) {
  // A native sub-agent (a Claude Code Task, a Codex collab thread) is bound to
  // its parent's `<vendor>-native-ui` row, so its agent name is an internal the
  // server itself hides (`public_agent_name`). Name the product instead,
  // matching the Agents rail and the composer. Otherwise prefer the session's
  // own `sub_agent_name`: a `sys_session_send` child is bound to its parent
  // bundle's agent row, so `boundAgent.name` would misidentify it as the
  // parent. `boundAgent.name` remains the fallback for the Add-Agent flow,
  // where the child is bound to its own agent and `sub_agent_name` is null.
  const subAgentSegment = isChildSession
    ? (nativeCodingAgentForSubagentWrapper(wrapperLabel)?.displayName ??
      (subAgentName?.trim() || null) ??
      nativeCodingAgentForAgentName(boundAgent?.name)?.displayName ??
      boundAgent?.name ??
      null)
    : null;
  const { data: hosts } = useHosts({ enabled: isChildSession && childSession != null });
  const childHostId = childSession?.host_id ?? null;
  const childHost = childHostId ? hosts?.find((host) => host.host_id === childHostId) : undefined;
  const childHostName = hostDisplayName(childHostId, childHost);
  const childDisplay = childSession ? childAgentDisplay(childSession) : null;
  // iOS/Android native chrome already identifies the session. Restore the
  // compact "< Back" climb-out there; web / Electron keep the parent name.
  const nativeMobileBack = isIOSShell() || isAndroidShell();
  return (
    <nav
      aria-label="Conversation"
      className={cn(
        "conversation-breadcrumb flex min-w-0 flex-wrap items-center gap-1.5 text-ui",
        className,
      )}
    >
      {projectTag ??
        (projectName && (
          <div className="hidden md:flex min-w-0 items-center gap-1.5 text-ui shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className={cn(
                    "breadcrumb-folder flex shrink-0 items-center text-muted-foreground hover:opacity-100",
                    // A full-color emoji reads as washed-out when faded, so only
                    // dim the monochrome folder fallback.
                    projectIcon ? "opacity-100" : "opacity-40",
                  )}
                  aria-label={`Project: ${projectName}`}
                >
                  <ProjectRowIcon icon={projectIcon} className="size-4 text-[16px]" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="bottom" align="start">
                <div>
                  <span className="font-semibold text-ui">{conversationTitle}</span>
                  <div className="flex gap-1 text-muted-foreground">
                    <ProjectRowIcon icon={projectIcon} className="size-4 text-[16px]" />
                    {projectName}
                  </div>
                </div>
              </TooltipContent>
            </Tooltip>
            <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
              /
            </span>
          </div>
        ))}
      {!isChildSession && <WorktreeStatusMark sessionId={sessionId} />}
      {titleLinkTo ? (
        <>
          <Link
            to={titleLinkTo}
            aria-label="Back to parent session"
            className={cn(
              "breadcrumb-parent-link min-w-0 text-muted-foreground hover:text-foreground",
              nativeMobileBack
                ? "inline-flex shrink-0 items-center gap-0.5"
                : "truncate hover:underline",
            )}
          >
            {nativeMobileBack ? (
              <>
                <ChevronLeftIcon className="size-4" />
                <span>Back</span>
              </>
            ) : (
              conversationTitle
            )}
          </Link>
          {/* The optional iOS title mode keeps Back as the actual navigation
              control while revealing a separately truncatable session title. */}
          <span
            aria-hidden
            className="breadcrumb-native-session-title min-w-0 truncate text-foreground"
          >
            {conversationTitle}
          </span>
        </>
      ) : (
        (titleSlot ?? (
          <span className="breadcrumb-session-title min-w-0 truncate text-foreground">
            {conversationTitle}
          </span>
        ))
      )}
      {actions}
      {isChildSession && childSession ? (
        <>
          <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
            /
          </span>
          <span className="flex min-w-0 items-center gap-1.5">
            <RailAgentBadge child={childSession} />
            <WorktreeStatusMark sessionId={sessionId} />
            <span
              data-testid="breadcrumb-child-label"
              className="truncate font-semibold text-foreground"
            >
              {childPrimaryLabel(childSession)}
            </span>
          </span>
          {childDisplay && (
            <ChildPlacementChip
              display={childDisplay}
              hostName={childHostName}
              cwd={childCwd ?? null}
            />
          )}
        </>
      ) : (
        isChildSession && (
          <>
            <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
              /
            </span>
            <span className="flex min-w-0 items-center gap-1.5">
              <BotIcon className="size-4 shrink-0 text-muted-foreground" />
              <WorktreeStatusMark sessionId={sessionId} />
              <span className="truncate font-semibold text-foreground">
                {subAgentSegment ?? "Sub-agent"}
              </span>
            </span>
          </>
        )
      )}
    </nav>
  );
}
