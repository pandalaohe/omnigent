import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";

import { isHexColor } from "@/lib/customTheme";
import { cn } from "@/lib/utils";

interface HsvColor {
  hue: number;
  saturation: number;
  value: number;
}

const FALLBACK_HSV: HsvColor = { hue: 258, saturation: 0.63, value: 0.96 };

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function hexToHsv(hex: string): HsvColor {
  const red = Number.parseInt(hex.slice(1, 3), 16) / 255;
  const green = Number.parseInt(hex.slice(3, 5), 16) / 255;
  const blue = Number.parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;

  let hue = 0;
  if (delta !== 0) {
    if (max === red) hue = 60 * (((green - blue) / delta) % 6);
    else if (max === green) hue = 60 * ((blue - red) / delta + 2);
    else hue = 60 * ((red - green) / delta + 4);
  }

  return {
    hue: hue < 0 ? hue + 360 : hue,
    saturation: max === 0 ? 0 : delta / max,
    value: max,
  };
}

export function hsvToHex({ hue, saturation, value }: HsvColor): string {
  const normalizedHue = ((hue % 360) + 360) % 360;
  const chroma = value * saturation;
  const segment = normalizedHue / 60;
  const secondary = chroma * (1 - Math.abs((segment % 2) - 1));
  const offset = value - chroma;

  let red: number;
  let green: number;
  let blue: number;
  if (segment < 1) [red, green, blue] = [chroma, secondary, 0];
  else if (segment < 2) [red, green, blue] = [secondary, chroma, 0];
  else if (segment < 3) [red, green, blue] = [0, chroma, secondary];
  else if (segment < 4) [red, green, blue] = [0, secondary, chroma];
  else if (segment < 5) [red, green, blue] = [secondary, 0, chroma];
  else [red, green, blue] = [chroma, 0, secondary];

  const channel = (component: number) =>
    Math.round((component + offset) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${channel(red)}${channel(green)}${channel(blue)}`;
}

export interface ColorSpectrumProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  testId?: string;
  spectrumClassName?: string;
}

/** Continuous 2D saturation/brightness pad paired with a 0-360° hue slider. */
export function ColorSpectrum({
  label,
  value,
  onChange,
  testId,
  spectrumClassName,
}: ColorSpectrumProps) {
  const activePointerIdRef = useRef<number | null>(null);
  const [hsv, setHsv] = useState<HsvColor>(() =>
    isHexColor(value) ? hexToHsv(value) : FALLBACK_HSV,
  );

  useEffect(() => {
    if (!isHexColor(value)) return;
    const target = value.toLowerCase();
    setHsv((prev) => {
      if (hsvToHex(prev) === target) return prev;
      const parsed = hexToHsv(target);
      // Preserve the active hue when editing black, white, or grey so the hue rail does not jump to red.
      return parsed.saturation === 0 ? { ...parsed, hue: prev.hue } : parsed;
    });
  }, [value]);

  const commit = useCallback(
    (next: HsvColor) => {
      setHsv(next);
      onChange(hsvToHex(next));
    },
    [onChange],
  );

  const updateFromPointer = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      commit({
        hue: hsv.hue,
        saturation: clamp((event.clientX - bounds.left) / bounds.width, 0, 1),
        value: 1 - clamp((event.clientY - bounds.top) / bounds.height, 0, 1),
      });
    },
    [commit, hsv.hue],
  );

  const releasePointer = (pointerId: number) => {
    if (activePointerIdRef.current === pointerId) activePointerIdRef.current = null;
  };

  const saturationPct = Math.round(hsv.saturation * 100);
  const brightnessPct = Math.round(hsv.value * 100);

  return (
    <div className="grid min-w-0 gap-2.5">
      <div
        role="slider"
        tabIndex={0}
        aria-label={`${label} saturation and brightness`}
        aria-roledescription="2D color slider"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={saturationPct}
        aria-valuetext={`Saturation ${saturationPct}%, brightness ${brightnessPct}%`}
        data-testid={testId ? `${testId}-spectrum` : undefined}
        onPointerDown={(event) => {
          if (activePointerIdRef.current !== null || !event.isPrimary || event.button !== 0) return;
          activePointerIdRef.current = event.pointerId;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          updateFromPointer(event);
        }}
        onPointerMove={(event) => {
          if (activePointerIdRef.current !== event.pointerId) return;
          if (
            (event.pointerType === "mouse" || event.pointerType === "pen") &&
            (event.buttons & 1) === 0
          ) {
            activePointerIdRef.current = null;
            return;
          }
          updateFromPointer(event);
        }}
        onPointerUp={(event) => releasePointer(event.pointerId)}
        onPointerCancel={(event) => releasePointer(event.pointerId)}
        onLostPointerCapture={(event) => releasePointer(event.pointerId)}
        onKeyDown={(event) => {
          const step = event.shiftKey ? 0.05 : 0.01;
          const deltas: Record<string, [number, number]> = {
            ArrowLeft: [-step, 0],
            ArrowRight: [step, 0],
            ArrowUp: [0, step],
            ArrowDown: [0, -step],
          };
          const delta = deltas[event.key];
          if (!delta) return;
          event.preventDefault();
          commit({
            hue: hsv.hue,
            saturation: clamp(hsv.saturation + delta[0], 0, 1),
            value: clamp(hsv.value + delta[1], 0, 1),
          });
        }}
        className={cn(
          "relative h-44 cursor-crosshair touch-none select-none overflow-hidden rounded-xl outline-none ring-1 ring-black/10 focus-visible:ring-2 focus-visible:ring-ring dark:ring-white/15",
          spectrumClassName,
        )}
        style={{
          backgroundColor: `hsl(${hsv.hue} 100% 50%)`,
          backgroundImage:
            "linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, transparent)",
        }}
      >
        <span
          aria-hidden="true"
          className="pointer-events-none absolute size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_1px_4px_rgb(0_0_0/0.75)] ring-1 ring-black/60"
          style={{
            left: `${hsv.saturation * 100}%`,
            top: `${(1 - hsv.value) * 100}%`,
          }}
        />
      </div>
      <input
        type="range"
        min="0"
        max="360"
        value={Math.round(hsv.hue)}
        aria-label={`${label} hue`}
        data-testid={testId ? `${testId}-hue` : undefined}
        onChange={(event) => commit({ ...hsv, hue: Number.parseInt(event.target.value, 10) })}
        className="h-3 w-full cursor-pointer appearance-none rounded-full bg-[linear-gradient(to_right,#ff3b30,#ffcc00,#34c759,#00c7be,#0a84ff,#bf5af2,#ff2d55,#ff3b30)] shadow-inner outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-moz-range-thumb]:size-5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-white [&::-moz-range-thumb]:bg-transparent [&::-moz-range-thumb]:shadow-md [&::-webkit-slider-thumb]:size-5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-white [&::-webkit-slider-thumb]:bg-transparent [&::-webkit-slider-thumb]:shadow-md"
      />
    </div>
  );
}
