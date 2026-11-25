import { ScheduleSelect as Select } from "./ScheduleSelect";
// Harness-specific automation controls; Default leaves the agent configuration unchanged.

import { normalizeSdkPermissionMode } from "@/lib/sdkPermissionModes";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { cn } from "@/lib/utils";
import { pickerTriggerClass } from "./pickerStyles";
import { Label } from "@/components/scheduled/Label";
import { SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { EFFORT_SELECT_NONE, MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import type { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";

/** Sentinel Select value for "no permission override" (use the agent default).
 * Distinct from Claude's real `default` (Manual) mode so leaving the control
 * alone keeps the agent's configured mode rather than forcing Manual. */
const PERMISSION_SELECT_DEFAULT = "__agent_default__";
/** Sentinel for an empty mode that inherits another session's, shown beside Default. */
const PERMISSION_SELECT_INHERITED = "__inherited__";

export function ModelEffortFields({
  model,
  effort,
  permissionMode,
  speed,
  settings,
  disabled = false,
  onModelChange,
  onEffortChange,
  onPermissionModeChange,
  onSpeedChange,
  onSelectOpenChange,
  idPrefix = "task",
  inheritedPermissionLabel,
  defaultPermissionMode = "",
}: {
  /** Selected model id/alias, or "" = agent default (nothing overridden). */
  model: string;
  /** Selected reasoning effort, or "" = agent default. */
  effort: string;
  /** Selected permission mode, or "" = agent default (nothing overridden). */
  permissionMode: string;
  speed: string;
  disabled?: boolean;
  settings: ReturnType<typeof useScheduledHarnessSettings>;
  onModelChange: (model: string) => void;
  onEffortChange: (effort: string) => void;
  onPermissionModeChange: (mode: string) => void;
  onSpeedChange: (speed: string) => void;
  /** Forwarded to each Select's onOpenChange so the parent Dialog can keep an
   * open dropdown from dismissing the whole modal. */
  onSelectOpenChange?: (open: boolean) => void;
  /** Prefix for element ids and test ids, so two groups can share a form. */
  idPrefix?: string;
  /** Offers an empty mode under this label because it inherits another session's. */
  inheritedPermissionLabel?: string;
  /** The mode Default saves; "" leaves the mode empty. A listed mode hides Default once left. */
  defaultPermissionMode?: string;
}) {
  const modelOptions = settings.models.map((option) => ({
    id: option.id,
    label: option.displayName ?? option.id,
  }));
  if (model && !modelOptions.some((option) => option.id === model)) {
    modelOptions.push({ id: model, label: model });
  }
  const effortOptions = [...settings.efforts];
  if (effort && !effortOptions.some((option) => option.value === effort)) {
    effortOptions.push({ value: effort, label: normalizeEffortLabel(effort) });
  }
  const permissionOptions = settings.permissions.filter(
    (option) =>
      ["claude-native", "claude-sdk"].includes(settings.harness ?? "") ||
      option.value !== "default",
  );
  const inheritsPermission = Boolean(inheritedPermissionLabel) && permissionMode === "";
  const permissionForSelectValue = (value: string) => {
    if (value === PERMISSION_SELECT_INHERITED) return "";
    return value === PERMISSION_SELECT_DEFAULT ? defaultPermissionMode : value;
  };
  let permissionSelectValue = normalizeSdkPermissionMode(settings.harness ?? "", permissionMode);
  if (inheritsPermission) permissionSelectValue = PERMISSION_SELECT_INHERITED;
  else if (
    permissionMode === "" ||
    (!["claude-native", "claude-sdk", "codex"].includes(settings.harness ?? "") &&
      permissionMode === "default")
  )
    permissionSelectValue = PERMISSION_SELECT_DEFAULT;
  // Once left, Default would duplicate a listed mode (Claude lists its default as Manual).
  const showsDefaultPermission =
    permissionSelectValue === PERMISSION_SELECT_DEFAULT ||
    !permissionOptions.some((option) => option.value === defaultPermissionMode);

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      {settings.supportsModel && (
        <div className="flex flex-col gap-3" data-testid={`${idPrefix}-model-effort-row`}>
          <div
            className="flex w-full min-w-0 items-center justify-between gap-4"
            data-testid={`${idPrefix}-model-control`}
          >
            <Label htmlFor={`${idPrefix}-model`}>Model</Label>
            <Select
              disabled={disabled}
              value={model === "" ? MODEL_SELECT_DEFAULT : model}
              componentId="tasks.scheduled.model"
              valueHasNoPii
              onValueChange={(v) => onModelChange(v === MODEL_SELECT_DEFAULT ? "" : v)}
              onOpenChange={onSelectOpenChange}
            >
              <SelectTrigger
                id={`${idPrefix}-model`}
                data-testid={`${idPrefix}-model-trigger`}
                className={cn(
                  "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                  pickerTriggerClass,
                )}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent
                position="popper"
                align="end"
                className="w-[min(20rem,calc(100vw-2rem))]"
              >
                <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
                {modelOptions.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {effortOptions.length > 0 && (
            <div
              className="flex w-full min-w-0 items-center justify-between gap-4"
              data-testid={`${idPrefix}-effort-control`}
            >
              <Label htmlFor={`${idPrefix}-effort`}>Effort</Label>
              <Select
                disabled={disabled}
                value={effort === "" ? EFFORT_SELECT_NONE : effort}
                componentId="tasks.scheduled.effort"
                valueHasNoPii
                onValueChange={(v) => onEffortChange(v === EFFORT_SELECT_NONE ? "" : v)}
                onOpenChange={onSelectOpenChange}
              >
                <SelectTrigger
                  id={`${idPrefix}-effort`}
                  data-testid={`${idPrefix}-effort-trigger`}
                  className={cn(
                    "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                    pickerTriggerClass,
                  )}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent
                  position="popper"
                  align="end"
                  className="w-[min(20rem,calc(100vw-2rem))]"
                >
                  <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
                  {effortOptions.map((e) => (
                    <SelectItem key={e.value} value={e.value}>
                      {e.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}
      {settings.catalogMessage && (
        <p className="text-sm text-muted-foreground">{settings.catalogMessage}</p>
      )}

      <AutomationSpeedField
        speed={speed}
        options={settings.speedOptions}
        disabled={disabled}
        onSpeedChange={onSpeedChange}
        onSelectOpenChange={onSelectOpenChange}
      />

      {permissionOptions.length > 0 && (
        <div
          className="flex w-full min-w-0 items-center justify-between gap-4"
          data-testid={`${idPrefix}-permission-control`}
        >
          <Label htmlFor={`${idPrefix}-permission`}>Permissions</Label>
          <Select
            disabled={disabled}
            value={permissionSelectValue}
            componentId="tasks.scheduled.permission_mode"
            valueHasNoPii
            onValueChange={(v) => onPermissionModeChange(permissionForSelectValue(v))}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id={`${idPrefix}-permission`}
              data-testid={`${idPrefix}-permission-trigger`}
              className={cn(
                "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                pickerTriggerClass,
              )}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="end"
              className="w-[min(20rem,calc(100vw-2rem))]"
            >
              {inheritedPermissionLabel && (
                <SelectItem value={PERMISSION_SELECT_INHERITED}>
                  {inheritedPermissionLabel}
                </SelectItem>
              )}
              {showsDefaultPermission && (
                <SelectItem value={PERMISSION_SELECT_DEFAULT}>Default</SelectItem>
              )}
              {permissionOptions.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}

export function AutomationSpeedField({
  speed,
  options,
  disabled,
  onSpeedChange,
  onSelectOpenChange,
}: {
  speed: string;
  options: ReturnType<typeof useScheduledHarnessSettings>["speedOptions"];
  disabled: boolean;
  onSpeedChange: (speed: string) => void;
  onSelectOpenChange?: (open: boolean) => void;
}) {
  if (!options.length) return null;
  return (
    <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-speed-control">
      <Label htmlFor="task-speed">Speed</Label>
      <Select
        disabled={disabled}
        value={speed || EFFORT_SELECT_NONE}
        onValueChange={(value) => onSpeedChange(value === EFFORT_SELECT_NONE ? "" : value)}
        onOpenChange={onSelectOpenChange}
      >
        <SelectTrigger id="task-speed" data-testid="task-speed-trigger" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
          <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
          {speed && !options.some((option) => option.value === speed) && (
            <SelectItem value={speed} disabled>
              Unavailable ({speed})
            </SelectItem>
          )}
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
