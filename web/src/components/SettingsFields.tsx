import { useEffect, useState, type ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
import { Input } from "@/components/ui/input";

interface NumericFieldProps {
  value: number | null;
  min: number;
  max: number;
  ariaLabel: string;
  disabled: boolean;
  placeholder?: string;
  onClear?: () => void;
  onCommit: (value: number) => void;
}

/**
 * Numeric input with a local draft: only a valid in-range integer is
 * committed while typing; blur restores the last committed value. With
 * `onClear` an empty input is allowed and reports the clear; without it an
 * empty input is ignored exactly as before.
 */
export function NumericField({
  value,
  min,
  max,
  ariaLabel,
  disabled,
  placeholder,
  onClear,
  onCommit,
}: NumericFieldProps) {
  const text = value === null ? "" : value.toString();
  const [draft, setDraft] = useState(text);

  useEffect(() => {
    setDraft(text);
  }, [text]);

  const update = (next: string) => {
    setDraft(next);
    if (next === "") {
      onClear?.();
      return;
    }
    if (!/^\d+$/.test(next)) return;
    const parsed = Number(next);
    if (parsed < min || parsed > max) return;
    onCommit(parsed);
  };

  return (
    <Input
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      step={1}
      aria-label={ariaLabel}
      disabled={disabled}
      placeholder={placeholder}
      value={draft}
      onChange={(event) => update(event.target.value)}
      onBlur={() => setDraft(text)}
      className="h-9 w-20"
    />
  );
}

export function SettingRow({
  label,
  hint,
  note,
  children,
}: {
  label: string;
  hint?: ReactNode;
  note?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      data-slot="setting-row"
      className="flex max-w-2xl flex-wrap items-center justify-between gap-x-6 gap-y-2"
    >
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="text-ui font-normal text-foreground md:font-medium">{label}</span>
          {hint != null && (
            <HelpTip label={`About ${label}`} className="size-11 md:size-4">
              {hint}
            </HelpTip>
          )}
        </div>
        {note != null && <div className="text-sm text-muted-foreground">{note}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}
