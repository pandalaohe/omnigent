import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import { agentBadgeFor } from "@/lib/agentBadgePreferences";
import { cn } from "@/lib/utils";

export interface AgentBadgeProps {
  agentId: string | null;
  className?: string;
}

/** Compact, optional visual identity for the Agent bound to a session. */
export function AgentBadge({ agentId, className }: AgentBadgeProps) {
  const preferences = useAgentBadgePreferences();
  const badge = agentBadgeFor(preferences, agentId);
  if (!badge) return null;

  return (
    <span
      aria-hidden="true"
      data-testid="agent-badge"
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
