import { useEffect, useMemo, useReducer, useState } from "react";
import { PlusIcon, RotateCcwIcon, Trash2Icon } from "lucide-react";

import { Kbd } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Switch } from "@/components/ui/switch";
import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  KEYBOARD_SHORTCUTS_CHANGED_EVENT,
  SHORTCUT_ACTION_IDS,
  currentShortcutPlatform,
  defaultShortcutBindings,
  deleteShortcutPlatformOverride,
  findShortcutConflicts,
  isShortcutActionEnabled,
  readKeyboardShortcutPreferences,
  shortcutBindingLabels,
  shortcutChordFromEvent,
  setShortcutRecordingActive,
  writeShortcutPreference,
  type ShortcutActionId,
  type ShortcutChord,
  type ShortcutGroupId,
  type ShortcutPlatform,
} from "@/lib/keyboardShortcutPreferences";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isNativeShell } from "@/lib/nativeBridge";

const GROUPS: { id: ShortcutGroupId; label: string; note?: string }[] = [
  { id: "general", label: "General" },
  { id: "chats", label: "In chats" },
  { id: "navigation", label: "Navigation" },
  { id: "view", label: "View" },
  { id: "slash", label: "Slash commands", note: "suggestions open" },
];

const PLATFORMS: { id: ShortcutPlatform; label: string }[] = [
  { id: "macos", label: "macOS" },
  { id: "windows", label: "Windows" },
  { id: "linux", label: "Linux" },
];

interface RecordingTarget {
  actionId: ShortcutActionId;
  platform: ShortcutPlatform | null;
}

function normalizeRecordedPrimary(chord: ShortcutChord): ShortcutChord {
  const platform = currentShortcutPlatform();
  const primaryModifier = platform === "macos" ? "meta" : "control";
  if (!chord.modifiers.includes(primaryModifier)) return chord;
  return {
    ...chord,
    modifiers: chord.modifiers.map((modifier) =>
      modifier === primaryModifier ? "primary" : modifier,
    ),
  };
}

function ShortcutBindingButton({
  actionId,
  platform,
  bindings,
  recording,
  onRecord,
}: {
  actionId: ShortcutActionId;
  platform: ShortcutPlatform | null;
  bindings: ShortcutChord[];
  recording: boolean;
  onRecord: () => void;
}) {
  const definition = DEFAULT_SHORTCUT_DEFINITIONS[actionId];
  const displayPlatform = platform ?? currentShortcutPlatform();
  const targetLabel = platform
    ? `${PLATFORMS.find((entry) => entry.id === platform)?.label} shortcut`
    : "common shortcut";
  return (
    <button
      type="button"
      onClick={onRecord}
      aria-label={`Record ${targetLabel} for ${definition.label}`}
      aria-pressed={recording}
      className="flex min-h-8 items-center justify-end gap-1 rounded-md px-1.5 outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
    >
      {recording ? (
        <span className="animate-pulse text-sm text-primary">Press shortcut…</span>
      ) : (
        bindings.flatMap((binding, bindingIndex) =>
          shortcutBindingLabels(binding, displayPlatform).map((label) => (
            <Kbd key={`${bindingIndex}-${label}`}>{label}</Kbd>
          )),
        )
      )}
    </button>
  );
}

function platformLabel(platform: ShortcutPlatform): string {
  return PLATFORMS.find((entry) => entry.id === platform)?.label ?? platform;
}

export function KeyboardShortcutEditor() {
  const [, refresh] = useReducer((version: number) => version + 1, 0);
  const [recording, setRecording] = useState<RecordingTarget | null>(null);
  const [error, setError] = useState<string | null>(null);
  const preferences = readKeyboardShortcutPreferences();
  const defaultContext = useMemo(
    () => ({
      submitWithModEnter: readSubmitWithModEnter(),
      nativeShell: isNativeShell(),
    }),
    [],
  );
  const defaultsFor = (actionId: ShortcutActionId) =>
    defaultShortcutBindings(actionId, defaultContext);

  const startRecording = (target: RecordingTarget) => {
    setError(null);
    setShortcutRecordingActive(true);
    setRecording(target);
  };

  useEffect(() => {
    const onChanged = () => refresh();
    window.addEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onChanged);
    window.addEventListener("storage", onChanged);
    return () => {
      window.removeEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onChanged);
      window.removeEventListener("storage", onChanged);
    };
  }, []);

  useEffect(() => {
    if (!recording) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing) return;
      const recordedChord = shortcutChordFromEvent(event);
      if (!recordedChord) return;
      const normalizedChord = normalizeRecordedPrimary(recordedChord);
      if (recording.actionId === "pinnedSession" && !/^Digit[0-9]$/.test(recordedChord.code)) {
        event.preventDefault();
        event.stopPropagation();
        setError("Jump to pinned session must use a number key from 1 to 0.");
        setShortcutRecordingActive(false);
        setRecording(null);
        return;
      }
      const chord =
        recording.actionId === "pinnedSession" && /^Digit[0-9]$/.test(recordedChord.code)
          ? { ...normalizedChord, code: "Digit*" }
          : normalizedChord;
      event.preventDefault();
      event.stopPropagation();

      const platforms = recording.platform
        ? [recording.platform]
        : (["macos", "windows", "linux"] as ShortcutPlatform[]);
      const conflicts = new Set<ShortcutActionId>();
      for (const platform of platforms) {
        for (const conflict of findShortcutConflicts(
          recording.actionId,
          [chord],
          platform,
          defaultContext,
        )) {
          conflicts.add(conflict);
        }
      }
      if (conflicts.size > 0) {
        const labels = [...conflicts].map(
          (actionId) => DEFAULT_SHORTCUT_DEFINITIONS[actionId].label,
        );
        setError(`That shortcut is already used by ${labels.join(", ")}.`);
        setShortcutRecordingActive(false);
        setRecording(null);
        return;
      }

      const current = readKeyboardShortcutPreferences().actions[recording.actionId] ?? {};
      if (recording.platform) {
        writeShortcutPreference(recording.actionId, {
          ...current,
          platformOverrides: {
            ...current.platformOverrides,
            [recording.platform]: [chord],
          },
        });
      } else {
        writeShortcutPreference(recording.actionId, { ...current, common: [chord] });
      }
      setError(null);
      setShortcutRecordingActive(false);
      setRecording(null);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      setShortcutRecordingActive(false);
    };
  }, [defaultContext, recording]);

  const definitionsByGroup = useMemo(
    () =>
      new Map(
        GROUPS.map((group) => [
          group.id,
          SHORTCUT_ACTION_IDS.map((actionId) => DEFAULT_SHORTCUT_DEFINITIONS[actionId]).filter(
            (definition) => definition.group === group.id,
          ),
        ]),
      ),
    [],
  );

  const addPlatformOverride = (actionId: ShortcutActionId, platform: ShortcutPlatform) => {
    const current = readKeyboardShortcutPreferences().actions[actionId] ?? {};
    writeShortcutPreference(actionId, {
      ...current,
      platformOverrides: {
        ...current.platformOverrides,
        [platform]: current.common ?? defaultsFor(actionId),
      },
    });
    setError(null);
  };

  const resetCommon = (actionId: ShortcutActionId) => {
    const current = readKeyboardShortcutPreferences().actions[actionId] ?? {};
    const { common: _removed, ...rest } = current;
    writeShortcutPreference(actionId, rest);
    setError(null);
  };

  const resetPlatform = (actionId: ShortcutActionId, platform: ShortcutPlatform) => {
    const current = readKeyboardShortcutPreferences().actions[actionId] ?? {};
    writeShortcutPreference(actionId, {
      ...current,
      platformOverrides: {
        ...current.platformOverrides,
        [platform]: defaultsFor(actionId),
      },
    });
    setError(null);
  };

  const setEnabled = (actionId: ShortcutActionId, enabled: boolean) => {
    const current = readKeyboardShortcutPreferences().actions[actionId] ?? {};
    const { enabled: _removed, ...rest } = current;
    writeShortcutPreference(actionId, enabled ? rest : { ...rest, enabled: false });
  };

  return (
    <div>
      {error ? (
        <p
          role="alert"
          className="mb-3 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      ) : null}
      {GROUPS.map((group) => (
        <section key={group.id} className="mb-5 last:mb-0">
          <h3 className="mb-1 text-sm font-medium text-muted-foreground">
            {group.label}
            {group.note ? (
              <span className="ml-1.5 font-normal text-muted-foreground/70">· {group.note}</span>
            ) : null}
          </h3>
          <ul>
            {(definitionsByGroup.get(group.id) ?? []).map((definition) => {
              const actionPreference = preferences.actions[definition.id];
              const platformOverrides = actionPreference?.platformOverrides ?? {};
              const missingPlatforms = PLATFORMS.filter(
                (platform) => platformOverrides[platform.id] === undefined,
              );
              const enabled = isShortcutActionEnabled(definition.id);
              return (
                <li
                  key={definition.id}
                  data-testid={`shortcut-editor-row-${definition.id}`}
                  className="border-b border-border/60 py-2 last:border-b-0"
                >
                  <div className="flex min-h-9 items-center gap-2">
                    <span className="min-w-0 flex-1 text-ui text-foreground">
                      {definition.label}
                    </span>
                    <Switch
                      checked={enabled}
                      onCheckedChange={(checked) => setEnabled(definition.id, checked)}
                      aria-label={`${enabled ? "Disable" : "Enable"} ${definition.label} shortcut`}
                      className="scale-90"
                    />
                    <ShortcutBindingButton
                      actionId={definition.id}
                      platform={null}
                      bindings={actionPreference?.common ?? defaultsFor(definition.id)}
                      recording={
                        recording?.actionId === definition.id && recording.platform === null
                      }
                      onRecord={() => startRecording({ actionId: definition.id, platform: null })}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Restore default shortcut for ${definition.label}`}
                      onClick={() => resetCommon(definition.id)}
                    >
                      <RotateCcwIcon />
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          disabled={missingPlatforms.length === 0}
                          aria-label={`Add system override for ${definition.label}`}
                        >
                          <PlusIcon />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {missingPlatforms.map((platform) => (
                          <DropdownMenuItem
                            key={platform.id}
                            onSelect={(event) => {
                              event.preventDefault();
                              addPlatformOverride(definition.id, platform.id);
                            }}
                          >
                            {platform.label}
                          </DropdownMenuItem>
                        ))}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                  {PLATFORMS.filter((platform) => platformOverrides[platform.id] !== undefined).map(
                    (platform) => (
                      <div
                        key={platform.id}
                        data-testid={`shortcut-platform-${definition.id}-${platform.id}`}
                        className="ml-5 flex min-h-9 items-center gap-2 border-l border-border/60 pl-3"
                      >
                        <span className="min-w-20 flex-1 text-sm text-muted-foreground">
                          {platform.label}
                        </span>
                        <ShortcutBindingButton
                          actionId={definition.id}
                          platform={platform.id}
                          bindings={platformOverrides[platform.id] ?? defaultsFor(definition.id)}
                          recording={
                            recording?.actionId === definition.id &&
                            recording.platform === platform.id
                          }
                          onRecord={() =>
                            startRecording({ actionId: definition.id, platform: platform.id })
                          }
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          aria-label={`Restore ${platform.label} shortcut for ${definition.label}`}
                          onClick={() => resetPlatform(definition.id, platform.id)}
                        >
                          <RotateCcwIcon />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          aria-label={`Delete ${platform.label} override for ${definition.label}`}
                          onClick={() => deleteShortcutPlatformOverride(definition.id, platform.id)}
                        >
                          <Trash2Icon />
                        </Button>
                      </div>
                    ),
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      <p
        className="mt-3 text-xs text-muted-foreground"
        title="Ctrl on Windows/Linux and Cmd on macOS are stored as the cross-platform primary modifier. System rows override the common shortcut; deleting one restores inheritance."
      >
        Click a shortcut to record.
      </p>
      <span className="sr-only">
        Current platform: {platformLabel(currentShortcutPlatform())}. Effective shortcuts update
        immediately.
      </span>
    </div>
  );
}
