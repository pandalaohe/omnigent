import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";

export function SessionDefaultModeSelect({
  harness,
  field,
  value,
  label,
  testId,
  disabled,
  onOpenChange,
  onChange,
}: {
  harness: string;
  field: "speed" | "permission";
  value: string | null | undefined;
  label: string;
  testId: string;
  disabled?: boolean;
  onOpenChange?: (open: boolean) => void;
  onChange: (value: string | null) => void;
}) {
  const options = sessionDefaultModeOptions(harness, field);
  if (options.length === 0) return null;
  return (
    <Select
      value={value ?? "__default__"}
      disabled={disabled}
      onOpenChange={onOpenChange}
      onValueChange={(next) => {
        if (next !== "") onChange(next === "__default__" ? null : next);
      }}
    >
      <SelectTrigger className="h-8 w-full min-w-0" aria-label={label} data-testid={testId}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        <SelectItem value="__default__">Default</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
