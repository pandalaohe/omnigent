import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import {
  agentBadgeFor,
  type AgentBadgePreferences,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { AGENT_TEMPLATE_LABEL } from "@/lib/customAgentsApi";
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import type { ChildSessionLike } from "@/shell/subagentRailGroups";
import { AgentBadgeMark } from "./AgentBadge";

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
 * The user-configured badge for a child, keyed like the sidebar. A bundled
 * member carries the bundle's bound agent row, and a harness sub-agent mirror
 * is another CLI's internal sub-agent, so neither wears a configured badge.
 */
export function childAgentBadge(
  child: ChildSessionLike,
  preferences: AgentBadgePreferences,
): AgentBadgeValue | null {
  if (child.sub_agent_name?.trim()) return null;
  if (nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]) != null) return null;
  return agentBadgeFor(
    preferences,
    child.labels?.[AGENT_TEMPLATE_LABEL] ?? child.agent_template_id ?? child.agent_id,
  );
}

/** The configured badge a rail row or child header would draw for this child. */
export function useChildAgentBadge(child: ChildSessionLike): AgentBadgeValue | null {
  return childAgentBadge(child, useAgentBadgePreferences());
}

export interface RailAgentBadgeProps {
  /** Child row or a session snapshot normalized to the snake_case shape. */
  child: ChildSessionLike;
  className?: string;
}

/** The child's configured agent badge, exactly as ``AgentBadge`` draws it. */
export function RailAgentBadge({ child, className }: RailAgentBadgeProps) {
  const badge = useChildAgentBadge(child);
  if (!badge) return null;

  return (
    <AgentBadgeMark
      badge={badge}
      className={className}
      title={childAgentDisplay(child) ?? undefined}
      data-testid="rail-agent-badge"
    />
  );
}
