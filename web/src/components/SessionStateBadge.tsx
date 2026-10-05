// Sidebar status indicator. Approval surfaces as a "Needs response" tag so
// it reads at a glance; other states stay compact. Verbose copy
// (incl. the approval count) lives in the tooltip.

import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { KeepWarmStatus } from "@/hooks/useConversations";
import type { SessionState } from "@/hooks/useSessionState";
import { keepWarmTooltipLine } from "@/lib/keepWarmStatus";
import { cn } from "@/lib/utils";
import type { ReactElement } from "react";
import { CircleAlertIcon } from "lucide-react";

export interface SessionStateBadgeProps {
  state: SessionState;
  /**
   * Render the cold keep-warm treatment (blue dot / blue framed tag) for the
   * unseen and awaiting states. Other states are unaffected.
   */
  cold?: boolean;
  /** Keep-warm status backing the appended cold tooltip line. */
  keepWarm?: KeepWarmStatus | null;
}

export interface ColdIdleDotProps {
  keepWarm?: KeepWarmStatus | null;
}

export interface BackgroundActivityBadgeProps {
  count: number;
}

export interface GoalActivityBadgeProps {
  state: "active" | "paused";
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => ReactElement;
  /** Whether the cold keep-warm styling applies to this state. */
  cold: boolean;
}

function describe(state: SessionState, cold: boolean): Visual {
  switch (state.kind) {
    case "awaiting": {
      const tooltip =
        state.count === 1 ? "1 approval prompt waiting" : `${state.count} approval prompts waiting`;
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        cold,
        render: () => (
          <Badge
            className={cn(
              cold
                ? "border-keep-cold bg-keep-cold/15 text-keep-cold"
                : "border-transparent bg-brand-accent/15 text-brand-accent",
            )}
          >
            Needs response
          </Badge>
        ),
      };
    }
    case "running":
      return {
        kind: state.kind,
        ariaLabel: "Session running",
        tooltip: "Session running",
        cold: false,
        render: () => <RunningDot className="size-3" />,
      };
    case "starting":
      // Same spinner as running — the session is coming up, not yet working.
      return {
        kind: state.kind,
        ariaLabel: "Session starting up",
        tooltip: "Session starting up",
        cold: false,
        render: () => <RunningDot className="size-3" />,
      };
    case "error":
      return {
        kind: state.kind,
        ariaLabel: "Latest message is an error",
        tooltip: "Latest message is an error",
        cold: false,
        render: () => (
          <CircleAlertIcon aria-hidden className="size-3.5 shrink-0 text-destructive" />
        ),
      };
    case "disconnected":
      return {
        kind: state.kind,
        ariaLabel: "Host disconnected",
        tooltip: "Host disconnected",
        cold: false,
        render: () => (
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full border border-muted-foreground"
          />
        ),
      };
    case "unseen":
      // Solid dot — brand pink normally, keep-cold blue when the cache is
      // cold. Distinguished from the running indicator (a grey spinner).
      return {
        kind: state.kind,
        ariaLabel: "New messages",
        tooltip: "New messages",
        cold,
        render: () => <Dot tone={cold ? "bg-keep-cold" : "bg-brand-accent"} />,
      };
  }
}

function Dot({ tone }: { tone: string }) {
  return <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", tone)} />;
}

export function SessionStateBadge({
  state,
  cold = false,
  keepWarm = null,
}: SessionStateBadgeProps) {
  const visual = describe(state, cold);
  const coldLine = visual.cold ? keepWarmTooltipLine(keepWarm) : null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="session-state-badge"
          data-state={visual.kind}
          data-cold={visual.cold ? "true" : undefined}
          role="img"
          aria-label={visual.ariaLabel}
          className="inline-flex h-5 shrink-0 items-center justify-center"
        >
          {visual.render()}
        </span>
      </TooltipTrigger>
      {/* Opens left: the badge sits at the right edge of the narrow
          sidebar, so a right-opening tooltip would overflow the panel. */}
      <TooltipContent side="left">
        {visual.tooltip}
        {coldLine !== null && <span className="block">{coldLine}</span>}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Idle-cold marker: the blue dot shown when a session's keep-warm cache is
 * cold but it has no other state to surface. Occupies the same fixed slot
 * as the unseen dot so compact-marker sizing lines up.
 */
export function ColdIdleDot({ keepWarm = null }: ColdIdleDotProps) {
  const label = keepWarmTooltipLine(keepWarm);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="session-state-badge"
          data-state="cold"
          data-cold="true"
          role="img"
          aria-label={label}
          className="inline-flex h-5 shrink-0 items-center justify-center"
        >
          <Dot tone="bg-keep-cold" />
        </span>
      </TooltipTrigger>
      <TooltipContent side="left">{label}</TooltipContent>
    </Tooltip>
  );
}

export function BackgroundActivityBadge({ count }: BackgroundActivityBadgeProps) {
  const label = `${count} background activit${count === 1 ? "y" : "ies"} running`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="background-activity-badge"
          role="img"
          aria-label={label}
          className="inline-flex size-4 shrink-0 items-center justify-center rounded border border-info/45 bg-info/10 font-semibold text-[10px] text-info leading-none"
        >
          B
        </span>
      </TooltipTrigger>
      <TooltipContent side="left">{label}</TooltipContent>
    </Tooltip>
  );
}

export function GoalActivityBadge({ state }: GoalActivityBadgeProps) {
  const active = state === "active";
  const label = active ? "Goal active" : "Goal paused";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="goal-activity-badge"
          data-state={state}
          role="img"
          aria-label={label}
          className={cn(
            "inline-flex size-4 shrink-0 items-center justify-center rounded border font-semibold text-[10px] leading-none",
            active
              ? "border-status-green/55 bg-status-green/10 text-status-green"
              : "border-status-yellow/55 bg-status-yellow/10 text-status-yellow",
          )}
        >
          G
        </span>
      </TooltipTrigger>
      <TooltipContent side="left">{label}</TooltipContent>
    </Tooltip>
  );
}
