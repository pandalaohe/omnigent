import { useEffect, useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  isAgentBadgeHexColor,
  isAgentBadgeTextColor,
  validateAgentBadgeLabel,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { cn } from "@/lib/utils";

const INITIAL_BORDER_COLOR = "#8b5cf6";
const INITIAL_TEXT_COLOR = "theme";
const OUTLINE_COLORS = [
  { name: "Red", value: "#ef4444" },
  { name: "Orange", value: "#ea580c" },
  { name: "Gold", value: "#ca8a04" },
  { name: "Green", value: "#16a34a" },
  { name: "Cyan", value: "#0891b2" },
  { name: "Blue", value: "#3b82f6" },
  { name: "Purple", value: "#8b5cf6" },
  { name: "Pink", value: "#db2777" },
];

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
              placeholder="A or 助"
              className="w-28"
            />
          </label>
          {labelError ? (
            <p id={errorId} className="-mt-3 text-xs text-destructive">
              {labelError}
            </p>
          ) : null}

          <div className="grid gap-2">
            <span className="text-sm">Outline presets</span>
            <div
              role="group"
              aria-label="Outline presets"
              className="grid grid-cols-4 gap-2 sm:grid-cols-8"
            >
              {OUTLINE_COLORS.map((color) => (
                <button
                  key={color.value}
                  type="button"
                  aria-label={`${color.name} outline`}
                  aria-pressed={borderColor.toLowerCase() === color.value}
                  title={color.name}
                  onClick={() => updateBorderColor(color.value)}
                  className={cn(
                    "flex h-11 min-w-0 items-center justify-center rounded-lg border-2 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                    borderColor.toLowerCase() === color.value
                      ? "border-foreground bg-accent"
                      : "border-transparent hover:bg-accent",
                  )}
                >
                  <span className="size-7 rounded-md" style={{ backgroundColor: color.value }} />
                </button>
              ))}
            </div>
          </div>
          <div className="grid min-w-0 gap-4">
            <ColorField
              label="Outline color"
              value={borderColor}
              valid={borderColorValid}
              onChange={updateBorderColor}
            />
            <div className="grid gap-2">
              <span className="text-sm">Text color</span>
              <div role="group" aria-label="Text color mode" className="grid grid-cols-2 gap-2">
                {[
                  { label: "Follow theme", value: "theme" },
                  { label: "Custom", value: "custom" },
                ].map((mode) => (
                  <button
                    key={mode.value}
                    type="button"
                    aria-pressed={(textColor === "theme") === (mode.value === "theme")}
                    onClick={() =>
                      updateTextColor(
                        mode.value === "theme"
                          ? "theme"
                          : textColor === "theme"
                            ? "#e9d5ff"
                            : textColor,
                      )
                    }
                    className={cn(
                      "min-h-11 rounded-lg border px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      (textColor === "theme") === (mode.value === "theme")
                        ? "border-foreground bg-accent text-foreground"
                        : "border-border text-muted-foreground hover:bg-accent",
                    )}
                  >
                    {mode.label}
                  </button>
                ))}
              </div>
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
