// The board's one reading mark: a ring whose thick arc is used, in the state's
// colour, and whose thin outer arc is time elapsed, in the time colour. Inside,
// the window name, then the whole percent large and its decimals small. A glyph
// beside a row keeps only the used arc. A window that only carries an amount
// gets a plain figure; a window with no reading gets a dashed ring and a dash.

import { cn } from "@/lib/utils";
import type { CSSProperties } from "react";
import { UsageName } from "./UsageName";

import type { UsageWindowView } from "./usageBoardData";
import {
  figureEms,
  formatPercentReading,
  formatRingPercent,
  isPercentWindow,
  ringFigureSize,
  splitPercentReading,
  stateTone,
  TONE_STROKE,
  windowName,
  type UsageTone,
} from "./usageBoardFormat";
import { UsageFigure } from "./UsageFigure";

// Geometry in viewBox units (0–100): time arc outside, used arc inside.
// The time dot (radius 3.4) still paints inside the ring's box.
const TIME_RADIUS = 46.5;
const TIME_STROKE = 3;
const TIME_DOT = 3.4;
const USAGE_RADIUS = 39;
const USAGE_STROKE = 8.5;
// A glyph (row-sized ring) draws one thick used arc and no time arc.
const GLYPH_RADIUS = 36;
const GLYPH_STROKE = 24;
const GLYPH_MAX = 32;
// Clear radius for text: the used arc's inner edge less a 3-unit margin.
const CLEAR_RADIUS = USAGE_RADIUS - USAGE_STROKE / 2 - 3;
// Inner stack, in percent of the ring: name, whole percent, decimals.
const NAME_SIZE = 11.5;
const WHOLE_SIZE = 28.5;
const DECIMAL_SIZE = 12.2;
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
  /** Write the window name inside: above the figure, or alone in a small ring. */
  showName?: boolean;
  className?: string;
}

export function toneForRing(window: UsageWindowView, stale: boolean): UsageTone {
  return stale ? "mute" : stateTone(window.state);
}

export function UsageRing({
  window,
  stale = false,
  size = 96,
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
  const glyph = size < GLYPH_MAX;
  const small = size < 64;
  const elapsed = window ? elapsedFraction(window) : 0;
  const reading =
    percent === null || glyph
      ? ""
      : small
        ? formatPercentReading(percent)
        : formatRingPercent(percent);
  // A small ring holds either its name (the compact list) or a bare figure.
  const named = showName && Boolean(window) && !glyph;
  const figure = reading && !(small && named);
  const [whole, decimals] = splitPercentReading(reading);
  // The whole percent shrinks only when it would leave the clear circle.
  const wholeSize = small
    ? ringFigureSize(figureEms(reading), 0, CLEAR_RADIUS, 30, 1000 / size)
    : Math.min(WHOLE_SIZE, (2 * Math.sqrt(CLEAR_RADIUS ** 2 - 9 ** 2)) / figureEms(whole));
  const nameTop = small ? 8 : 26;
  const nameWidth = 2 * Math.sqrt(Math.max(0, CLEAR_RADIUS ** 2 - nameTop ** 2));
  // The name shrinks to its clear width, down to 10 px; a small ring's name may grow to 24 %.
  const nameSize = window
    ? Math.max(
        1000 / size,
        Math.min(small ? 24 : NAME_SIZE, nameWidth / figureEms(windowName(window))),
      )
    : NAME_SIZE;

  return (
    <span
      className={cn("usage-reading-figure", className)}
      style={{ "--usage-ring-size": `${size}px` } as CSSProperties}
    >
      <span
        className="usage-ring"
        data-testid="usage-ring"
        aria-label={reading ? `${reading}% used` : undefined}
        data-tone={tone}
        data-state={window?.state ?? "none"}
        data-stale={stale || undefined}
        data-glyph={glyph || undefined}
      >
        <svg viewBox="0 0 100 100" aria-hidden="true" className="usage-ring-art">
          {percent === null ? (
            <circle
              cx={50}
              cy={50}
              r={42}
              fill="none"
              className="usage-ring-dash"
              strokeWidth={3}
              strokeDasharray="3 5"
              strokeLinecap="round"
            />
          ) : glyph ? (
            <>
              <circle
                cx={50}
                cy={50}
                r={GLYPH_RADIUS}
                fill="none"
                className="usage-ring-track"
                strokeWidth={GLYPH_STROKE}
              />
              {arc(GLYPH_RADIUS, percent / 100, color, GLYPH_STROKE, false)}
            </>
          ) : (
            <>
              <circle
                cx={50}
                cy={50}
                r={TIME_RADIUS}
                fill="none"
                className="usage-ring-track usage-ring-time-track"
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
              {arc(USAGE_RADIUS, percent / 100, color, USAGE_STROKE, false)}
              {arc(TIME_RADIUS, elapsed, "var(--usage-time)", TIME_STROKE, true)}
              {elapsed > 0 ? (
                <circle
                  cx={50 + TIME_RADIUS * Math.sin(TAU * elapsed)}
                  cy={50 - TIME_RADIUS * Math.cos(TAU * elapsed)}
                  r={TIME_DOT}
                  fill="var(--usage-time)"
                />
              ) : null}
            </>
          )}
        </svg>
        {!glyph ? (
          <span className="usage-ring-content" data-small={small || undefined}>
            {named && window ? (
              <UsageName
                data-usage-label
                className="usage-ring-name"
                style={{ fontSize: `${nameSize}cqi`, maxWidth: `${nameWidth.toFixed(1)}cqi` }}
              >
                {windowName(window)}
              </UsageName>
            ) : null}
            {figure ? (
              small ? (
                <span
                  className="usage-ring-percent"
                  style={{ fontSize: `${wholeSize.toFixed(1)}cqi` }}
                  data-usage-percent=""
                >
                  {reading}
                </span>
              ) : (
                <span className="usage-ring-percent" data-usage-percent="stack">
                  <span
                    className="usage-ring-whole"
                    style={{ fontSize: `${wholeSize.toFixed(1)}cqi` }}
                  >
                    {whole}
                  </span>
                  <span
                    className="usage-ring-decimals"
                    style={{ fontSize: `max(10px, ${DECIMAL_SIZE}cqi)` }}
                  >
                    {decimals}%
                  </span>
                </span>
              )
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
