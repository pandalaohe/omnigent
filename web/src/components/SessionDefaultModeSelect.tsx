import { CircleHelpIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  sessionDefaultModeOptions,
  sessionModeHandler,
  type HarnessCapabilityMap,
} from "@/lib/sessionDefaultModes";
import { normalizeAgyPermissionTier } from "@/lib/nativeHarnessModes";
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
  capabilities,
  onOpenChange,
  onChange,
  inheritLabel = "Default",
}: {
  harness: string;
  field: "speed" | "permission";
  value: string | null | undefined;
  label: string;
  testId: string;
  disabled?: boolean;
  models?: readonly NativeModelOption[];
  model?: string | null;
  capabilities?: HarnessCapabilityMap;
  onOpenChange?: (open: boolean) => void;
  onChange: (value: string | null) => void;
  inheritLabel?: string;
}) {
  const options = sessionDefaultModeOptions(harness, field, models, model, "default", capabilities);
  if (options.length === 0) return null;
  const tierDefault =
    field === "permission" && sessionModeHandler(harness, field, capabilities) === "agy-tier";
  const legacyTier = tierDefault ? normalizeAgyPermissionTier(value) : null;
  const selected = tierDefault ? legacyTier : value === "priority" ? "fast" : value;
  const available = selected == null || options.some((option) => option.value === selected);
  const select =
    options.length > COMBOBOX_OPTION_THRESHOLD ? (
      <fieldset disabled={disabled} className="contents">
        <div data-testid={testId} className="min-w-0 flex-1">
          <Combobox
            mode="select"
            options={[
              {
                key: "__default__",
                label: inheritLabel,
                description: inheritLabel === "Default" ? undefined : "Default",
              },
              ...(!available && selected
                ? [{ key: selected, label: `Unavailable (${value})`, disabled: true }]
                : []),
              ...options.map((option) => ({ key: option.value, label: option.label })),
            ]}
            value={selected ?? "__default__"}
            onOpenChange={onOpenChange}
            onValueChange={(next) => onChange(next === "__default__" ? null : next)}
            aria-label={label}
            className="h-auto w-full rounded-none! bg-transparent p-0"
          />
        </div>
      </fieldset>
    ) : (
      <Select
        value={selected ?? "__default__"}
        disabled={disabled}
        onOpenChange={onOpenChange}
        onValueChange={(next) => {
          if (next !== "") onChange(next === "__default__" ? null : next);
        }}
      >
        <SelectTrigger className="h-8 w-full min-w-0" aria-label={label} data-testid={testId}>
          <SelectValue>{selected == null ? inheritLabel : undefined}</SelectValue>
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
  if (tierDefault && ["auto-strict", "auto-standard"].includes(value ?? "")) {
    return (
      <div className="min-w-0 flex-1">
        {select}
        <p role="status" className="mt-1 text-xs text-muted-foreground">
          Legacy Auto modes now use the fixed, relaxed rules for risky actions.
        </p>
      </div>
    );
  }
  if (field !== "speed" || !["claude-native", "claude-sdk"].includes(harness)) return select;
  return (
    <div className="flex min-w-0 items-center gap-1">
      {select}
      <ClaudeFastModeHelp />
    </div>
  );
}

export function ClaudeFastModeHelp() {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button type="button" variant="ghost" size="icon-xs" aria-label="About Claude fast mode">
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
  );
}
