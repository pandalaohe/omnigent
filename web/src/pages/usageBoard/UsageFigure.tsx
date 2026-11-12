// An amount reading — an amount-only window or a balance — as a plain figure
// in the same slot a ring takes, so the rows of a tile stay aligned. The figure
// sizes itself to its slot's width and never wraps; a tile shortens six-digit
// amounts while the title keeps the full value.

import { cn } from "@/lib/utils";
import type { CSSProperties } from "react";
import { figureEms, formatTileAmount, formatUsageValue } from "./usageBoardFormat";
import { UsageName } from "./UsageName";

/** Split a formatted figure into its unit marks and the number between them. */
export function splitFigure(text: string): { prefix: string; number: string; suffix: string } {
  const match = /^([^\d<-]*)(.*?)((?:\s+\S+)?)$/.exec(text);
  return match
    ? { prefix: match[1], number: match[2], suffix: match[3] }
    : { prefix: "", number: text, suffix: "" };
}

export function FigureText({ text, className }: { text: string; className?: string }) {
  if (!/\d/.test(text)) return <span className={className}>{text}</span>;
  const { prefix, number, suffix } = splitFigure(text);
  return (
    <span className={className}>
      {prefix ? (
        <span className="usage-unit-mark" data-lead="">
          {prefix}
        </span>
      ) : null}
      {number}
      {suffix ? <span className="usage-unit-mark">{suffix}</span> : null}
    </span>
  );
}

interface UsageFigureProps {
  label?: string;
  value: number | null;
  unit: string | null;
  /** Height of the slot, the same as a ring's diameter beside it. */
  size?: number;
  /** Write every digit, as the detail page does. */
  full?: boolean;
  className?: string;
}

export function UsageFigure({
  label,
  value,
  unit,
  size = 88,
  full = false,
  className,
}: UsageFigureProps) {
  const exact = value === null ? "" : formatUsageValue(value, unit);
  const text = value === null ? "—" : full ? exact : formatTileAmount(value, unit);
  const { prefix, number, suffix } = splitFigure(text);
  const fit = 92 / figureEms(number, prefix + suffix);
  return (
    <span
      className={cn("usage-figure", className)}
      style={{ "--usage-ring-size": `${size}px`, "--usage-figure-fit": fit } as CSSProperties}
      data-testid="usage-figure"
    >
      <span className="usage-figure-body">
        {label ? (
          <UsageName data-usage-label className="usage-figure-label">
            {label}
          </UsageName>
        ) : null}
        <span className="usage-figure-value" data-usage-amount title={exact || undefined}>
          <FigureText text={text} />
        </span>
      </span>
    </span>
  );
}
