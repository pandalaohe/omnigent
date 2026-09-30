import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import { agentBadgeFor, type AgentBadgeValue } from "@/lib/agentBadgePreferences";
import { cn } from "@/lib/utils";

export interface AgentBadgeProps {
  agentId: string | null;
  className?: string;
}

export interface AgentBadgeMarkProps {
  badge: AgentBadgeValue;
  className?: string;
  title?: string;
  "data-testid"?: string;
}

/** Presentational mark shared by every surface that draws a configured badge. */
export function AgentBadgeMark({
  badge,
  className,
  title,
  "data-testid": testId,
}: AgentBadgeMarkProps) {
  return (
    <span
      aria-hidden="true"
      title={title}
      data-testid={testId}
      className={cn(
        "inline-flex size-5 shrink-0 items-center justify-center rounded-[5px] border-2 text-[10px] leading-none font-semibold tracking-[-0.03em]",
        className,
      )}
      style={{
        borderColor: badge.borderColor,
        color: badge.textColor === "theme" ? "var(--foreground)" : badge.textColor,
      }}
    >
      {badge.label}
    </span>
  );
}

/** Compact, optional visual identity for the Agent bound to a session. */
export function AgentBadge({ agentId, className }: AgentBadgeProps) {
  const preferences = useAgentBadgePreferences();
  const badge = agentBadgeFor(preferences, agentId);
  if (!badge) return null;

  return <AgentBadgeMark badge={badge} className={className} data-testid="agent-badge" />;
}
