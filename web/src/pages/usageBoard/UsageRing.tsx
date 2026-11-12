// The board's one reading mark: a ring whose inner arc is used and whose thin
// outer arc is time elapsed, in the window's own colour. A window that only
// carries an amount gets a dashed ring and the figure; a window with no
// reading at all gets the dashed ring and nothing — never a `0`.

import { cn } from "@/lib/utils";
import type { CSSProperties } from "react";
import { useUsageReadingPlacement } from "./useUsageReadingPlacement";
import { UsageName } from "./UsageName";

import type { UsageWindowView } from "./usageBoardData";
import {
  formatUsageValue,
  isPercentWindow,
  stateTone,
  TONE_STROKE,
  windowName,
  type UsageTone,
} from "./usageBoardFormat";

const RADIUS = 40;
const STROKE = 8.5;
const OUTER_RADIUS = 47.6;
const OUTER_STROKE = 3;
const CIRCUMFERENCE = Math.PI * 2;

function arc(radius: number, fraction: number, color: string, stroke: number) {
  const length = CIRCUMFERENCE * radius;
  const drawn = Math.max(0, Math.min(1, fraction)) * length;
  return (
    <circle
      cx={50}
      cy={50}
      r={radius}
      fill="none"
      stroke={color}
      strokeWidth={stroke}
      strokeLinecap="butt"
      strokeDasharray={`${drawn.toFixed(2)} ${(length + 2).toFixed(2)}`}
      transform="rotate(-90 50 50)"
    />
  );
}

/** Fraction of the window's time that has already passed. */
function elapsedFraction(window: UsageWindowView): number {
  const minutes = window.window_minutes;
  if (!minutes || window.time_left_s === null) return 0;
  return Math.max(0, Math.min(1, 1 - window.time_left_s / (minutes * 60)));
}

interface UsageRingProps {
  /** Null when the account has never been read. */
  window: UsageWindowView | null;
  /** The whole account's reading is old: no colour, no pace claim. */
  stale?: boolean;
  /** Maximum diameter in px; the containing tile may make it smaller. */
  size?: number;
  /** Write the window name above the figure, as the board does. */
  showName?: boolean;
  className?: string;
}

export function toneForRing(window: UsageWindowView, stale: boolean): UsageTone {
  return stale ? "mute" : stateTone(window.state);
}

export function UsageRing({
  window,
  stale = false,
  size = 88,
  showName = true,
  className,
}: UsageRingProps) {
  const tone = window ? toneForRing(window, stale) : "mute";
  const color = TONE_STROKE[tone];
  const dashed = !window || !isPercentWindow(window);
  const percent = window && isPercentWindow(window) ? (window.percent_used ?? 0) : null;
  const percentText = percent === null ? "" : `${percent.toFixed(2)}%`;
  const elapsed = window ? elapsedFraction(window) : 0;
  const value =
    window && !isPercentWindow(window)
      ? window.used_value !== null && window.used_value !== undefined
        ? formatUsageValue(window.used_value, window.unit)
        : ""
      : "";
  const reading = percent !== null && size > 20 ? percentText : value;
  const { circleRef, readingRef, outside } = useUsageReadingPlacement(reading);
  const readingNode = reading ? (
    <span
      ref={readingRef}
      className={cn(
        "usage-ring-reading font-semibold",
        percent !== null ? "usage-ring-percent font-bold leading-none" : "text-foreground",
      )}
      style={
        percent !== null ? { fontSize: `max(10px, ${110 / percentText.length}cqw)` } : undefined
      }
      data-usage-amount={percent === null ? "" : undefined}
      data-usage-percent={percent !== null ? "" : undefined}
    >
      {reading}
    </span>
  ) : null;

  return (
    <span
      className={cn("usage-reading-figure", className)}
      style={{ "--usage-ring-size": `${size}px` } as CSSProperties}
      data-reading-outside={outside}
    >
      <span
        ref={circleRef}
        className="usage-ring relative inline-flex items-center justify-center"
        data-testid="usage-ring"
        data-tone={tone}
        data-state={window?.state ?? "none"}
      >
        <svg
          viewBox="-2 -2 104 104"
          aria-hidden="true"
          className="absolute inset-0 h-full w-full"
          style={{ opacity: stale ? 0.45 : 1 }}
        >
          {dashed ? (
            <circle
              cx={50}
              cy={50}
              r={44}
              fill="none"
              stroke={window ? color : "var(--foreground)"}
              strokeWidth={3}
              strokeDasharray="6 6"
              opacity={window ? 0.8 : 0.42}
            />
          ) : (
            <>
              <circle
                cx={50}
                cy={50}
                r={RADIUS}
                fill="none"
                stroke="var(--border)"
                strokeWidth={STROKE}
              />
              <circle
                cx={50}
                cy={50}
                r={OUTER_RADIUS}
                fill="none"
                stroke="var(--foreground)"
                strokeOpacity={0.08}
                strokeWidth={OUTER_STROKE}
              />
              {percent !== null ? arc(RADIUS, percent / 100, color, STROKE) : null}
              {window ? arc(OUTER_RADIUS, elapsed, "var(--info)", OUTER_STROKE) : null}
              {elapsed > 0 ? (
                <circle
                  cx={50 + OUTER_RADIUS * Math.sin(2 * Math.PI * elapsed)}
                  cy={50 - OUTER_RADIUS * Math.cos(2 * Math.PI * elapsed)}
                  r={3.4}
                  fill="var(--info)"
                />
              ) : null}
            </>
          )}
        </svg>
        <span className="usage-ring-content relative flex flex-col items-center leading-none">
          {showName && window ? (
            <UsageName
              data-usage-label
              className="usage-ring-name mb-0.5 w-full px-1 text-center font-bold text-muted-foreground"
            >
              {windowName(window)}
            </UsageName>
          ) : null}
          {!outside ? readingNode : null}
        </span>
      </span>
      {outside ? readingNode : null}
    </span>
  );
}
