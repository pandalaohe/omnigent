import { useEffect, useState, type ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
import { Input } from "@/components/ui/input";

interface NumericFieldProps {
  value: number;
  min: number;
  max: number;
  ariaLabel: string;
  disabled: boolean;
  onCommit: (value: number) => void;
}

/**
 * Numeric input with a local draft: only a valid in-range integer is
 * committed while typing; blur restores the last committed value.
 */
export function NumericField({
  value,
  min,
  max,
  ariaLabel,
  disabled,
  onCommit,
}: NumericFieldProps) {
  const [draft, setDraft] = useState(value.toString());

  useEffect(() => {
    setDraft(value.toString());
  }, [value]);

  const update = (text: string) => {
    setDraft(text);
    if (!/^\d+$/.test(text)) return;
    const parsed = Number(text);
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
      value={draft}
      onChange={(event) => update(event.target.value)}
      onBlur={() => setDraft(value.toString())}
      className="h-9 w-20"
    />
  );
}

export function SettingRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="text-sm font-medium text-foreground">{label}</span>
        <HelpTip label={`About ${label}`}>{hint}</HelpTip>
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}
