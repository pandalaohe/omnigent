import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useContextIndicatorMode } from "@/hooks/useContextIndicatorMode";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { formatTokenCount } from "@/lib/formatCost";
import {
  resolveUsageContext,
  usageContextSourceFromKey,
  usageContextLevelLabel,
} from "@/lib/usageContextPreferences";
import { cn } from "@/lib/utils";

/** Circumference of the progress ring (r=5.5). */
const RING_CIRCUMFERENCE = 2 * Math.PI * 5.5;

/**
 * Compact context-usage ring for the composer workspace bar.
 *
 * Grayscale by design — a near-full context window is a neutral fact, not an
 * error, so the ring never escalates to warning/destructive colors. Self-nulls
 * when there is nothing to show, so the landing/pre-session window (no context
 * figures) renders nothing.
 *
 * @param contextWindow - Total context window in tokens; ``<= 0``/``null``
 *   renders nothing (``0/0`` would be ``NaN%``).
 * @param tokensUsed - Tokens consumed so far, or ``null`` when unknown.
 */
export function ComposerContextRing({
  contextWindow,
  tokensUsed,
  autoCompactTokenLimit = null,
  sourceKey = "",
  className,
}: {
  contextWindow: number | null;
  tokensUsed: number | null;
  autoCompactTokenLimit?: number | null;
  sourceKey?: string;
  className?: string;
}) {
  const mode = useContextIndicatorMode();
  const preferences = useUsageContextPreferences();
  const limits = resolveUsageContext(preferences, sourceKey, {
    contextWindow,
    autoCompactTokenLimit,
  });
  const source = usageContextSourceFromKey(sourceKey);
  const labels = {
    hostLabel: source?.hostId || "Server",
    agentLabel: source?.agentName || source?.harness || "Agent",
  };
  const compactMode =
    mode === "compact" && limits.autoCompactTokenLimit != null && limits.autoCompactTokenLimit > 0;
  const denominator = compactMode ? limits.autoCompactTokenLimit : limits.contextWindow.value;
  if (
    limits.contextWindow.value == null ||
    limits.contextWindow.value <= 0 ||
    denominator == null ||
    denominator <= 0 ||
    tokensUsed == null
  )
    return null;

  const pct = Math.min(tokensUsed / denominator, 1);
  // Arc, %, label, and tooltip all encode context USED: a fresh session
  // shows an empty ring at 0% and the ring fills as context is consumed.
  const usedArc = pct * RING_CIRCUMFERENCE;
  const usedPct = Math.round(pct * 100);
  const budget = compactMode ? "compact budget" : "context";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          data-testid="composer-context-ring"
          className={cn(
            "flex shrink-0 items-center rounded-full bg-transparent p-0 text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            className,
          )}
          aria-label={`${usedPct}% of ${budget} used`}
        >
          {/* Tight stroke bounds keep the visible icon-to-label gap consistent. */}
          <svg viewBox="1.5 1.5 13 13" width="13" height="13" fill="none" aria-hidden="true">
            {/* Track */}
            <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="2" opacity="0.2" />
            {/* Used arc — skipped at 0, where round linecaps would still paint a dot. */}
            {usedArc > 0 && (
              <circle
                cx="8"
                cy="8"
                r="5.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeDasharray={`${usedArc} ${RING_CIRCUMFERENCE}`}
                transform="rotate(-90 8 8)"
              />
            )}
          </svg>
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="flex-col items-start gap-0 px-3 py-2 text-left text-sm">
        <p className="tabular-nums text-ui leading-tight">
          {usedPct}% {budget} used
        </p>
        <p className="tabular-nums text-neutral-400 leading-tight dark:text-muted-foreground">
          {formatTokenCount(tokensUsed)} / {formatTokenCount(denominator)} tokens
        </p>
        <p className="text-xs text-muted-foreground">
          Context · {usageContextLevelLabel(limits.contextWindow.level, labels)}
        </p>
        <p className="text-xs text-muted-foreground">
          Buffer {limits.buffer.value?.toLocaleString() ?? "Auto"}
          {limits.buffer.mode === "percent" && limits.buffer.value != null ? "%" : ""} ·{" "}
          {usageContextLevelLabel(limits.buffer.level, labels)}
        </p>
        {limits.ignored.map((item) => (
          <p key={item.level} className="text-xs text-muted-foreground">
            Ignored {usageContextLevelLabel(item.level, labels)}: {item.reason}; using{" "}
            {usageContextLevelLabel(limits.buffer.level, labels)}.
          </p>
        ))}
      </TooltipContent>
    </Tooltip>
  );
}
