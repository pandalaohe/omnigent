import { AgentBadgeMark } from "@/components/AgentBadge";
import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import { harnessSection } from "@/hooks/useAgentRegistry";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { agentBadgeFor } from "@/lib/agentBadgePreferences";
import { cn } from "@/lib/utils";

export interface AgentNameTagProps {
  agentId: string | null;
  name: string;
  harness: string | null;
  markOnly?: boolean;
  className?: string;
}

function plainMark(harness: string | null): string {
  if (harness === "pi" || harness === "pi-rpc") return "RPC";
  const sdk = harness && (harness.endsWith("-sdk") || Object.hasOwn(BRAIN_HARNESS_LABELS, harness));
  const section = harnessSection(sdk ? "cli-subprocess" : null, harness);
  if (section === "native") return "Native";
  if (section === "sdk") return "SDK";
  if (section === "acp") return "ACP";
  return "Other";
}

/** Agent name with the saved badge, or one consistent integration-type mark. */
export function AgentNameTag({
  agentId,
  name,
  harness,
  markOnly = false,
  className,
}: AgentNameTagProps) {
  const preferences = useAgentBadgePreferences();
  const badge = agentBadgeFor(preferences, agentId);

  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5", className)}>
      {!markOnly && <span className="min-w-0 wrap-anywhere">{name}</span>}
      {badge ? (
        <AgentBadgeMark badge={badge} data-testid="agent-name-badge" />
      ) : (
        <span
          aria-hidden="true"
          data-testid="agent-name-mark"
          className="inline-flex shrink-0 items-center rounded border border-border px-1 text-[11px] leading-4 font-normal text-muted-foreground"
        >
          {plainMark(harness)}
        </span>
      )}
    </span>
  );
}
