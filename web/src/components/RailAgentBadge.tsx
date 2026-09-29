import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import { useHostColorPreferences } from "@/hooks/useHostColorPreferences";
import { agentBadgeFor, type AgentBadgePreferences } from "@/lib/agentBadgePreferences";
import { AGENT_TEMPLATE_LABEL } from "@/lib/customAgentsApi";
import { hostColor } from "@/lib/hostColors";
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { cn } from "@/lib/utils";
import type { ChildSessionLike } from "@/shell/subagentRailGroups";

function nativeAgentForChild(child: ChildSessionLike) {
  return (
    nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]) ??
    nativeCodingAgentForHarness(child.harness) ??
    nativeCodingAgentForAgentName(child.agent_name)
  );
}

/**
 * Display name for a child's agent: the native vendor product name (wrapper,
 * then harness, then bound agent name), else the bundled member's own name
 * before the bundle row's, else the sub-agent type. Mirrors the breadcrumb's
 * member-before-bundle ordering.
 */
export function childAgentDisplay(child: ChildSessionLike): string | null {
  const nativeAgent = nativeAgentForChild(child);
  if (nativeAgent) return nativeAgent.displayName;
  return child.sub_agent_name?.trim() || child.agent_name?.trim() || child.tool || null;
}

/**
 * Badge letters for a child. A user-configured badge label wins for a
 * directly bound agent, but not for a bundled member (whose configured row
 * belongs to the bundle). Otherwise native vendors get their product
 * initials and everything else the first two letters of its display name.
 */
export function childAgentBadgeLetters(
  child: ChildSessionLike,
  badgePreferences: AgentBadgePreferences,
): string {
  if (!child.sub_agent_name?.trim()) {
    const badgeKey = child.labels?.[AGENT_TEMPLATE_LABEL] ?? child.agent_id ?? null;
    const configured = agentBadgeFor(badgePreferences, badgeKey);
    if (configured) return configured.label;
  }
  const nativeAgent = nativeAgentForChild(child);
  if (nativeAgent?.key === "claude") return "CC";
  if (nativeAgent?.key === "codex") return "CX";
  const display = childAgentDisplay(child);
  if (!display) return "?";
  return display.slice(0, 2).toUpperCase();
}

export interface RailAgentBadgeProps {
  /** Child row or a session snapshot normalized to the snake_case shape. */
  child: ChildSessionLike;
  /** Effective host id; defaults to the child's own ``host_id``. */
  hostId?: string | null;
  /** Resolved host display name, used for the automatic colour hash. */
  hostName?: string | null;
  className?: string;
}

/**
 * Small square agent badge for the Agents rail and the child page header:
 * letters identify the agent, border and text colour identify the host.
 */
export function RailAgentBadge({ child, hostId, hostName, className }: RailAgentBadgeProps) {
  const badgePreferences = useAgentBadgePreferences();
  const colorPreferences = useHostColorPreferences();
  const display = childAgentDisplay(child);
  const color = hostColor(hostId ?? child.host_id, hostName, colorPreferences);
  return (
    <span
      data-testid="rail-agent-badge"
      title={display ?? undefined}
      className={cn(
        "inline-flex h-4 min-w-[22px] shrink-0 items-center justify-center rounded-[4px] border-[1.5px] px-[3px] text-[10px] leading-none font-bold",
        className,
      )}
      style={{ borderColor: color.hex, color: color.hex }}
    >
      {childAgentBadgeLetters(child, badgePreferences)}
    </span>
  );
}
