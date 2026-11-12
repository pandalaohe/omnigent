// The board's one reading mark: a ring whose thick arc is used, in the state's
// colour, and whose thin outer arc is time elapsed, in a neutral tone. A window
// that only carries an amount gets a plain figure; a window with no reading at
// all gets a dashed ring and a dash — never a `0`.

import { cn } from "@/lib/utils";
import type { CSSProperties } from "react";
import { UsageName } from "./UsageName";

import type { UsageWindowView } from "./usageBoardData";
import {
  figureEms,
  formatPercentReading,
  isPercentWindow,
  ringFigureSize,
  stateTone,
  TONE_STROKE,
  windowName,
  type UsageTone,
} from "./usageBoardFormat";
import { UsageFigure } from "./UsageFigure";

// Geometry in viewBox units (0–100): time track outside, usage track inside.
const TIME_RADIUS = 47.5;
const TIME_STROKE = 3;
const USAGE_RADIUS = 41;
const USAGE_STROKE = 7;
// Clear radius for text: the usage track's inner edge less a 3-unit margin.
const CLEAR_RADIUS = USAGE_RADIUS - USAGE_STROKE / 2 - 3;
// Window-name line inside the ring: its height plus the gap above the figure.
const NAME_BLOCK = 15;
const TAU = Math.PI * 2;

function arc(radius: number, fraction: number, color: string, stroke: number, round: boolean) {
  const length = TAU * radius;
  const drawn = Math.max(0, Math.min(1, fraction)) * length;
  return (
    <circle
      cx={50}
      cy={50}
      r={radius}
      fill="none"
      stroke={color}
      strokeWidth={stroke}
      strokeLinecap={round && fraction < 0.995 ? "round" : "butt"}
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
  const percent = window && isPercentWindow(window) ? (window.percent_used ?? 0) : null;
  // An amount-only window is a figure, not a ring, once there is room to read it.
  if (window && percent === null && size >= 64) {
    return (
      <UsageFigure
        className={className}
        label={showName ? windowName(window) : undefined}
        value={window.used_value ?? null}
        unit={window.unit}
        size={size}
      />
    );
  }
  const elapsed = window ? elapsedFraction(window) : 0;
  const reading = percent !== null && size > 20 ? formatPercentReading(percent) : "";
  const named = showName && Boolean(window) && size > 40;
  const figure = reading ? ringFigureSize(figureEms(reading, "%"), named ? NAME_BLOCK : 0) : 0;
  const nameTop = ((named ? NAME_BLOCK : 0) + 0.74 * figure) / 2;
  const nameWidth = 2 * Math.sqrt(Math.max(0, CLEAR_RADIUS ** 2 - nameTop ** 2));

  return (
    <span
      className={cn("usage-reading-figure", className)}
      style={{ "--usage-ring-size": `${size}px` } as CSSProperties}
    >
      <span
        className="usage-ring"
        data-testid="usage-ring"
        data-tone={tone}
        data-state={window?.state ?? "none"}
        data-stale={stale || undefined}
      >
        <svg viewBox="0 0 100 100" aria-hidden="true" className="usage-ring-art">
          {percent === null ? (
            <circle
              cx={50}
              cy={50}
              r={USAGE_RADIUS}
              fill="none"
              className="usage-ring-track"
              strokeWidth={2}
              strokeDasharray="5 5"
            />
          ) : (
            <>
              <circle
                cx={50}
                cy={50}
                r={TIME_RADIUS}
                fill="none"
                className="usage-ring-track"
                strokeWidth={TIME_STROKE}
              />
              <circle
                cx={50}
                cy={50}
                r={USAGE_RADIUS}
                fill="none"
                className="usage-ring-track"
                strokeWidth={USAGE_STROKE}
              />
              {arc(USAGE_RADIUS, percent / 100, color, USAGE_STROKE, true)}
              {arc(TIME_RADIUS, elapsed, "var(--usage-time)", TIME_STROKE, false)}
              {elapsed > 0 && size > 20 ? (
                <circle
                  cx={50 + TIME_RADIUS * Math.sin(TAU * elapsed)}
                  cy={50 - TIME_RADIUS * Math.cos(TAU * elapsed)}
                  r={2.5}
                  fill="var(--usage-time)"
                />
              ) : null}
            </>
          )}
        </svg>
        {size > 20 ? (
          <span className="usage-ring-content">
            {named && window ? (
              <UsageName
                data-usage-label
                className="usage-ring-name"
                style={{ maxWidth: `${nameWidth.toFixed(1)}cqi` }}
              >
                {windowName(window)}
              </UsageName>
            ) : null}
            {reading ? (
              <span
                className="usage-ring-percent"
                style={{ fontSize: `${figure}cqi` }}
                data-usage-percent=""
              >
                {reading}
                <span className="usage-unit-mark">%</span>
              </span>
            ) : !window ? (
              <span className="usage-ring-none" aria-label="No reading">
                —
              </span>
            ) : null}
          </span>
        ) : null}
      </span>
    </span>
  );
}
