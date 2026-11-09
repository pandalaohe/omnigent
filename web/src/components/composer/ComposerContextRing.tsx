import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useContextIndicatorMode } from "@/hooks/useContextIndicatorMode";
import { useHoverPinFloat } from "@/hooks/useHoverPinFloat";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { formatTokenCount } from "@/lib/formatCost";
import { useNavigate } from "@/lib/routing";
import { resolveUsageContext, usageContextSettingsHref } from "@/lib/usageContextPreferences";
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
 * The float reads the numbers and offers one entry; it edits nothing. A hover
 * shows it, a tap pins it (phones have no hover), and a click beside it or
 * Escape clears it, like every `?` in settings.
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
  const { open, setOpen, triggerHandlers, contentHandlers } = useHoverPinFloat();
  const navigate = useNavigate();
  const limits = resolveUsageContext(preferences, sourceKey, {
    contextWindow,
    autoCompactTokenLimit,
  });
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
  // Arc, %, label, and float all encode context USED: a fresh session shows an
  // empty ring at 0% and the ring fills as context is consumed.
  const usedArc = pct * RING_CIRCUMFERENCE;
  const usedPct = Math.round(pct * 100);
  const budget = compactMode ? "compact budget" : "context";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="composer-context-ring"
          className={cn(
            "relative flex size-6 shrink-0 items-center justify-center rounded-full bg-transparent p-0 text-muted-foreground after:absolute after:-inset-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 focus-visible:ring-offset-background md:after:inset-0",
            className,
          )}
          aria-label={`${usedPct}% of ${budget} used`}
          {...triggerHandlers}
          onClick={(event) => {
            // A click pins the float; Escape or a click beside it clears it.
            event.preventDefault();
            setOpen(true);
          }}
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
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        data-pop=""
        data-testid="composer-context-float"
        className="w-[min(18rem,calc(100vw-1rem))] flex-col items-stretch gap-2 text-sm"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        {...contentHandlers}
      >
        <p className="tabular-nums text-ui leading-tight">
          {usedPct}% {budget} used
        </p>
        <p className="tabular-nums text-neutral-400 leading-tight dark:text-muted-foreground">
          {formatTokenCount(tokensUsed)} / {formatTokenCount(denominator)} tokens
        </p>
        <Button
          type="button"
          size="sm"
          className="mt-1 min-h-11 w-full md:min-h-8"
          onClick={() => {
            setOpen(false);
            navigate(usageContextSettingsHref(sourceKey));
          }}
        >
          Change context limits
        </Button>
      </PopoverContent>
    </Popover>
  );
}
