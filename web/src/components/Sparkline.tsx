// Tiny inline-SVG trend line. The monitor draws many of these (one per host
// plus the server), and recharts' responsive container + resize observers are
// far too heavy for a 40 px strip — a single polyline is enough.

import { cn } from "@/lib/utils";

interface SparklineProps {
  /** Values in chronological order; an empty list renders nothing. */
  points: number[];
  /**
   * Value at which to draw the dashed threshold line, or `null` for none.
   * The line is horizontal; the y-scale covers the data and the threshold.
   */
  threshold?: number | null;
  /**
   * Label for the threshold line, drawn in a right gutter the line never
   * reaches. Display only: the line is set from the metric it belongs to.
   */
  thresholdLabel?: string;
  /** Accessible name; when omitted the chart is decorative. */
  label?: string;
  className?: string;
  /**
   * Renders a dot on the latest point when set: live (muted) or stale
   * (amber). Omitted, the chart renders exactly as before.
   */
  latest?: "live" | "stale";
}

export function Sparkline({
  points,
  threshold = null,
  thresholdLabel,
  label,
  className,
  latest,
}: SparklineProps) {
  if (points.length === 0) return null;
  // Scale from zero: a CPU/memory series that never nears its ceiling should
  // still read as "low", not as a full-height line.
  const maxValue = Math.max(...points, threshold ?? 0, 1);
  const y = (value: number) => (100 - (value / maxValue) * 100).toFixed(2);
  const coords = points
    .map((value, index) => {
      const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100;
      return `${x.toFixed(2)},${y(value)}`;
    })
    .join(" ");
  const ariaLabel =
    label !== undefined && thresholdLabel !== undefined
      ? `${label}. Alert line ${thresholdLabel}`
      : label;
  const chart = (
    <svg
      role={label ? "img" : undefined}
      aria-label={ariaLabel}
      aria-hidden={label ? undefined : true}
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      className="h-10 w-full"
    >
      {threshold !== null && (
        <line
          x1="0"
          x2="100"
          y1={y(threshold)}
          y2={y(threshold)}
          className="stroke-amber-500"
          strokeWidth="1"
          strokeDasharray="3 3"
          vectorEffect="non-scaling-stroke"
        />
      )}
      <polyline
        points={coords}
        fill="none"
        className="stroke-foreground/60"
        strokeWidth="1.5"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
  const body =
    latest === undefined ? (
      chart
    ) : (
      <div className="relative">
        {chart}
        <span
          aria-hidden="true"
          data-testid="sparkline-latest"
          className={cn(
            "absolute size-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full",
            latest === "live" ? "bg-foreground/60" : "bg-amber-500",
          )}
          style={{ top: `${y(points[points.length - 1])}%`, left: `${lastX(points.length)}%` }}
        />
      </div>
    );
  const showThresholdLabel = threshold !== null && thresholdLabel !== undefined;
  // Keep the label inside the 40 px gutter even when the line sits on the top
  // or bottom edge of the plot.
  const lineTop = Math.min(88, Math.max(12, Number(y(threshold ?? 0))));
  return (
    <div className={cn("flex items-start gap-1", className)} data-testid="sparkline">
      <div className="min-w-0 flex-1">{body}</div>
      {showThresholdLabel && (
        // The gutter is a sibling of the plot, so a label can never sit over
        // the line it names.
        <div className="relative h-10 w-10 shrink-0" aria-hidden="true">
          <span
            data-testid="sparkline-threshold-label"
            className="absolute right-0 -translate-y-1/2 text-[10px] leading-none tabular-nums text-amber-700 dark:text-amber-400"
            style={{ top: `${lineTop}%` }}
          >
            {thresholdLabel}
          </span>
        </div>
      )}
    </div>
  );
}

/** The last dot sits at x = 50 for a single point and x = 100 for a series. */
function lastX(pointCount: number): number {
  return pointCount === 1 ? 50 : 100;
}
