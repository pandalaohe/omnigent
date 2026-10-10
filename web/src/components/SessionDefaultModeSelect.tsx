import { CircleHelpIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";
import type { NativeModelOption } from "@/lib/types";

const EMPTY_MODELS: readonly NativeModelOption[] = [];

export function SessionDefaultModeSelect({
  harness,
  field,
  value,
  label,
  testId,
  disabled,
  models = EMPTY_MODELS,
  model = null,
  onOpenChange,
  onChange,
}: {
  harness: string;
  field: "speed" | "permission";
  value: string | null | undefined;
  label: string;
  testId: string;
  disabled?: boolean;
  models?: readonly NativeModelOption[];
  model?: string | null;
  onOpenChange?: (open: boolean) => void;
  onChange: (value: string | null) => void;
}) {
  const options = sessionDefaultModeOptions(harness, field, models, model);
  if (options.length === 0) return null;
  const selected = value === "priority" ? "fast" : value;
  const available = selected == null || options.some((option) => option.value === selected);
  const select = (
    <Select
      value={selected ?? "__default__"}
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
        {!available && selected && (
          <SelectItem value={selected} disabled>
            Unavailable ({value})
          </SelectItem>
        )}
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
  if (field !== "speed" || !["claude-native", "claude-sdk"].includes(harness)) return select;
  return (
    <div className="flex min-w-0 items-center gap-1">
      {select}
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="About Claude fast mode"
            >
              <CircleHelpIcon className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            Fast mode supports Opus 5.5, 5 and 4.8, subject to account availability. Other models
            start at standard speed. On subscription plans it uses usage credits instead of included
            plan usage. First enable charges the conversation context at the uncached fast rate;
            starting fast minimizes that charge.
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </div>
  );
}
