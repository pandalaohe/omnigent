import { Segmented } from "@/components/ui/segmented";
import { ChevronRightIcon } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { AgentBadgeMark } from "@/components/AgentBadge";
import { PickerShell } from "@/components/PickerShell";
import { ColorSpectrum } from "@/components/theme/ColorSpectrum";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  isAgentBadgeHexColor,
  isAgentBadgeTextColor,
  validateAgentBadgeLabel,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { cn } from "@/lib/utils";

const INITIAL_BORDER_COLOR = "#8b5cf6";
const INITIAL_TEXT_COLOR = "theme";

// 8 hues × 5 Tailwind 300–700 shades (light → dark); retains all 8 original outline colors.
const OUTLINE_SHADES = ["Lightest", "Light", "Medium", "Dark", "Darkest"] as const;
const OUTLINE_HUES: readonly {
  name: string;
  shades: readonly [string, string, string, string, string];
}[] = [
  { name: "Red", shades: ["#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c"] },
  { name: "Orange", shades: ["#fdba74", "#fb923c", "#f97316", "#ea580c", "#c2410c"] },
  { name: "Gold", shades: ["#fde047", "#facc15", "#eab308", "#ca8a04", "#a16207"] },
  { name: "Green", shades: ["#86efac", "#4ade80", "#22c55e", "#16a34a", "#15803d"] },
  { name: "Cyan", shades: ["#67e8f9", "#22d3ee", "#06b6d4", "#0891b2", "#0e7490"] },
  { name: "Blue", shades: ["#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8"] },
  { name: "Purple", shades: ["#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed", "#6d28d9"] },
  { name: "Pink", shades: ["#f9a8d4", "#f472b6", "#ec4899", "#db2777", "#be185d"] },
];

const OUTLINE_COLORS = OUTLINE_SHADES.flatMap((shade, row) =>
  OUTLINE_HUES.map((hue) => ({
    name: `${shade} ${hue.name}`,
    value: hue.shades[row],
  })),
);

function normalizeHexInput(raw: string): string {
  const trimmed = raw.trim();
  if (/^#[0-9a-f]{6}$/i.test(trimmed)) return trimmed;
  return /^[0-9a-f]{6}$/i.test(trimmed) ? `#${trimmed}` : raw;
}

export interface AgentBadgeEditorProps {
  value: AgentBadgeValue | null;
  onChange: (value: AgentBadgeValue | null) => void;
  onValidityChange?: (valid: boolean) => void;
  className?: string;
}

export function AgentBadgeEditor({
  value,
  onChange,
  onValidityChange,
  className,
}: AgentBadgeEditorProps) {
  const labelId = useId();
  const errorId = `${labelId}-error`;
  const colorId = `${labelId}-color`;
  const mobile = useIsMobileViewport();
  const [colorSheetOpen, setColorSheetOpen] = useState(false);
  const [showBadge, setShowBadge] = useState(value !== null);
  const [label, setLabel] = useState(value?.label ?? "");
  const [borderColor, setBorderColor] = useState(value?.borderColor ?? INITIAL_BORDER_COLOR);
  const [textColor, setTextColor] = useState(value?.textColor ?? INITIAL_TEXT_COLOR);

  useEffect(() => {
    if (value) {
      setShowBadge(true);
      setLabel(value.label);
      setBorderColor(value.borderColor);
      setTextColor(value.textColor);
    } else {
      setShowBadge(false);
    }
  }, [value]);

  useEffect(() => {
    if (!mobile) setColorSheetOpen(false);
  }, [mobile]);

  const labelError = showBadge ? validateAgentBadgeLabel(label) : null;
  const borderColorValid = isAgentBadgeHexColor(borderColor);
  const textColorValid = isAgentBadgeTextColor(textColor);
  const valid = !showBadge || (labelError === null && borderColorValid && textColorValid);

  useEffect(() => onValidityChange?.(valid), [onValidityChange, valid]);

  const emitIfValid = (next: { label: string; borderColor: string; textColor: string }) => {
    if (
      validateAgentBadgeLabel(next.label) === null &&
      isAgentBadgeHexColor(next.borderColor) &&
      isAgentBadgeTextColor(next.textColor)
    ) {
      onChange({
        label: next.label.trim(),
        borderColor: next.borderColor.toLowerCase(),
        textColor: next.textColor.toLowerCase(),
      });
    }
  };

  const updateLabel = (nextLabel: string) => {
    setLabel(nextLabel);
    emitIfValid({ label: nextLabel, borderColor, textColor });
  };
  const updateBorderColor = (nextColor: string) => {
    setBorderColor(nextColor);
    emitIfValid({ label, borderColor: nextColor, textColor });
  };
  const updateTextColor = (nextColor: string) => {
    setTextColor(nextColor);
    emitIfValid({ label, borderColor, textColor: nextColor });
  };

  const matchedPreset = OUTLINE_COLORS.find((color) => color.value === borderColor.toLowerCase());

  const colorPickerControls = (inSheet: boolean) => (
    <div
      className={cn(
        "grid min-w-0",
        inSheet ? "gap-3" : "grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_11.5rem] md:items-start",
      )}
    >
      <div
        role="group"
        aria-label={inSheet ? "Badge color" : undefined}
        aria-labelledby={inSheet ? undefined : colorId}
        className={cn("grid min-w-0 grid-cols-8", inSheet ? "gap-1.5" : "gap-1")}
      >
        {OUTLINE_COLORS.map((color) => {
          const selected = borderColor.toLowerCase() === color.value;
          return (
            <button
              key={color.value}
              type="button"
              aria-label={`${color.name} outline`}
              aria-pressed={selected}
              title={`${color.name} (${color.value.toUpperCase()})`}
              onClick={() => updateBorderColor(color.value)}
              className={cn(
                "flex aspect-square w-full min-w-0 items-center justify-center rounded-md border-2 p-0.5 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background",
                selected
                  ? "border-foreground bg-accent"
                  : "border-transparent hover:border-muted-foreground/50",
              )}
            >
              <span
                className="block size-full rounded-[3px] ring-1 ring-inset ring-black/10 dark:ring-white/10"
                style={{ backgroundColor: color.value }}
              />
            </button>
          );
        })}
      </div>
      <div className="grid min-w-0 gap-2.5">
        <ColorSpectrum
          label="Outline color"
          value={borderColor}
          onChange={updateBorderColor}
          testId="agent-badge-color"
          spectrumClassName={inSheet ? "h-24 rounded-lg" : "h-28 rounded-lg md:h-[106px]"}
        />
        <div className="flex min-w-0 items-center gap-2">
          <span
            aria-hidden="true"
            className="size-8 shrink-0 rounded-lg border border-input ring-1 ring-inset ring-black/10 dark:ring-white/10"
            style={{ backgroundColor: borderColorValid ? borderColor : "transparent" }}
          />
          <Input
            value={borderColor}
            onChange={(event) => updateBorderColor(normalizeHexInput(event.target.value))}
            aria-label="Outline color hex"
            aria-invalid={!borderColorValid}
            maxLength={7}
            placeholder="#8B5CF6"
            spellCheck={false}
            className="min-w-0 flex-1 font-mono uppercase"
          />
        </div>
      </div>
    </div>
  );

  return (
    <div className={cn("grid gap-4", className)} data-testid="agent-badge-editor">
      <div className="flex items-center justify-between gap-6">
        <label htmlFor={`${labelId}-enabled`} className="text-sm font-medium text-foreground">
          Show badge
        </label>
        <Switch
          id={`${labelId}-enabled`}
          checked={showBadge}
          onCheckedChange={(checked) => {
            setShowBadge(checked);
            if (checked) emitIfValid({ label, borderColor, textColor });
            else onChange(null);
          }}
          aria-label="Show badge"
        />
      </div>

      {showBadge ? (
        <div className="grid gap-4 border-t border-border pt-4">
          <label className="grid gap-1.5 text-sm" htmlFor={labelId}>
            Badge text
            <Input
              id={labelId}
              value={label}
              onChange={(event) => updateLabel(event.target.value)}
              aria-invalid={labelError !== null}
              aria-describedby={labelError ? errorId : undefined}
              autoComplete="off"
              spellCheck={false}
              placeholder="A or AI"
              className="w-28"
            />
          </label>
          {labelError ? (
            <p id={errorId} className="-mt-3 text-xs text-destructive">
              {labelError}
            </p>
          ) : null}

          <div className="grid min-w-0 gap-2">
            <div className="flex items-center gap-2">
              <span id={colorId} className="text-sm">
                Badge color
              </span>
              {valid && (
                <AgentBadgeMark
                  badge={{ label: label.trim(), borderColor, textColor }}
                  data-testid="agent-badge-preview"
                />
              )}
            </div>
            {mobile ? (
              <PickerShell
                title="Badge color"
                open={colorSheetOpen}
                onOpenChange={setColorSheetOpen}
                trigger={
                  <button
                    type="button"
                    aria-labelledby={colorId}
                    aria-invalid={!borderColorValid}
                    data-testid="agent-badge-color-trigger"
                    className={cn(
                      "flex h-10 w-full min-w-0 items-center gap-2.5 rounded-lg border bg-background px-2.5 text-left text-sm outline-none transition-colors hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring dark:bg-input",
                      borderColorValid ? "border-input" : "border-destructive",
                    )}
                  >
                    <span
                      aria-hidden="true"
                      className="size-6 shrink-0 rounded-md ring-1 ring-inset ring-black/10 dark:ring-white/10"
                      style={{ backgroundColor: borderColorValid ? borderColor : "transparent" }}
                    />
                    <span className="font-mono text-sm font-medium uppercase">
                      {borderColor.toUpperCase()}
                    </span>
                    {matchedPreset && (
                      <span className="min-w-0 truncate text-xs text-muted-foreground">
                        {matchedPreset.name}
                      </span>
                    )}
                    <ChevronRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground" />
                  </button>
                }
                footer={
                  <Button type="button" onClick={() => setColorSheetOpen(false)}>
                    Done
                  </Button>
                }
              >
                <div className="grid h-full gap-2.5 overflow-y-auto p-3.5">
                  {valid && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>Preview</span>
                      <AgentBadgeMark
                        badge={{ label: label.trim(), borderColor, textColor }}
                        data-testid="agent-badge-sheet-preview"
                      />
                    </div>
                  )}
                  {colorPickerControls(true)}
                </div>
              </PickerShell>
            ) : (
              colorPickerControls(false)
            )}
          </div>

          <div className="grid min-w-0 gap-4">
            <div className="grid gap-2">
              <span className="text-sm">Text color</span>
              <Segmented
                label="Text color mode"
                value={textColor === "theme" ? "theme" : "custom"}
                stretch
                options={[
                  { value: "theme", label: "Follow theme" },
                  { value: "custom", label: "Custom" },
                ]}
                onChange={(mode) =>
                  updateTextColor(
                    mode === "theme" ? "theme" : textColor === "theme" ? "#e9d5ff" : textColor,
                  )
                }
              />
              {textColor !== "theme" && (
                <ColorField
                  label="Text color"
                  value={textColor}
                  valid={textColorValid}
                  onChange={updateTextColor}
                />
              )}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ColorField({
  label,
  value,
  valid,
  onChange,
}: {
  label: string;
  value: string;
  valid: boolean;
  onChange: (value: string) => void;
}) {
  const id = useId();
  const pickerValue = valid ? value : "#000000";
  return (
    <div className="grid min-w-0 gap-1.5 text-sm">
      <span id={`${id}-label`}>{label}</span>
      <span className="flex min-w-0 items-center gap-2">
        <input
          type="color"
          value={pickerValue}
          onChange={(event) => onChange(event.target.value)}
          aria-label={`${label} picker`}
          className="size-8 shrink-0 cursor-pointer rounded-lg border border-input bg-transparent p-0.5"
        />
        <Input
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-label={`${label} hex`}
          aria-invalid={!valid}
          maxLength={7}
          spellCheck={false}
          className="min-w-0 font-mono uppercase"
        />
      </span>
    </div>
  );
}
