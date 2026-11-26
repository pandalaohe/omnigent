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
  kindTag?: NonNativeKind | null;
  className?: string;
}

type NonNativeKind = "SDK" | "ACP" | "RPC" | "Other";
type IntegrationKind = NonNativeKind | "Native";

function integrationKind(harness: string | null, integrationMode?: string | null): IntegrationKind {
  if (harness === "pi" || harness === "pi-rpc") return "RPC";
  const sdk = harness && (harness.endsWith("-sdk") || Object.hasOwn(BRAIN_HARNESS_LABELS, harness));
  const section = harnessSection(integrationMode ?? (sdk ? "cli-subprocess" : null), harness);
  if (section === "native") return "Native";
  if (section === "sdk") return "SDK";
  if (section === "acp") return "ACP";
  return "Other";
}

export function nonNativeAgentKind(
  harness: string | null,
  integrationMode?: string | null,
): NonNativeKind | null {
  const kind = integrationKind(harness, integrationMode);
  return kind === "Native" ? null : kind;
}

/** The small outlined mark beside an agent's name. */
export const AGENT_MARK_CLASS =
  "inline-flex shrink-0 items-center rounded border border-border px-1 text-[11px] leading-4 font-normal text-muted-foreground";

/** The integration-type mark: Native, SDK, ACP, RPC or Other. */
export function AgentIntegrationMark({
  harness,
  className,
}: {
  harness: string | null;
  className?: string;
}) {
  return <IntegrationMark label={integrationKind(harness)} className={className} />;
}

function IntegrationMark({ label, className }: { label: IntegrationKind; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-testid="agent-name-mark"
      className={cn(AGENT_MARK_CLASS, className)}
    >
      {label}
    </span>
  );
}

/** Agent name with a saved badge. Tables can supply a separate kind tag. */
export function AgentNameTag({
  agentId,
  name,
  harness,
  markOnly = false,
  kindTag,
  className,
}: AgentNameTagProps) {
  const preferences = useAgentBadgePreferences();
  const badge = agentBadgeFor(preferences, agentId);
  const kind = kindTag === undefined && !badge ? integrationKind(harness) : kindTag;

  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5", className)}>
      {!markOnly && <span className="min-w-0 wrap-anywhere">{name}</span>}
      {badge && <AgentBadgeMark badge={badge} data-testid="agent-name-badge" />}
      {kind && <IntegrationMark label={kind} />}
    </span>
  );
}
