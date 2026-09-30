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
  /** Accessible name; when omitted the chart is decorative. */
  label?: string;
  className?: string;
  /**
   * Renders a dot on the latest point when set: live (muted) or stale
   * (amber). Omitted, the chart renders exactly as before.
   */
  latest?: "live" | "stale";
}

export function Sparkline({ points, threshold = null, label, className, latest }: SparklineProps) {
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
  const chart = (
    <svg
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      className={cn("h-10 w-full", latest === undefined ? className : undefined)}
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
  if (latest === undefined) return chart;
  // The wrapper carries the caller's margins; the dot sits on the last point,
  // which the polyline centers (single point) or ends at the right edge.
  const lastX = points.length === 1 ? 50 : 100;
  return (
    <div className={cn("relative", className)}>
      {chart}
      <span
        aria-hidden="true"
        data-testid="sparkline-latest"
        className={cn(
          "absolute size-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full",
          latest === "live" ? "bg-foreground/60" : "bg-amber-500",
        )}
        style={{ top: `${y(points[points.length - 1])}%`, left: `${lastX}%` }}
      />
    </div>
  );
}
