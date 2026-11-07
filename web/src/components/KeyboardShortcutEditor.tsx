import { useEffect, useMemo, useState } from "react";
import { PlusIcon, RotateCcwIcon, Trash2Icon } from "lucide-react";

import { Kbd, useKeyboardShortcutsVersion } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { SettingRow } from "@/components/SettingsFields";
import { SessionNavigationSettings } from "@/components/SessionNavigationSettings";
import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  SHORTCUT_ACTION_IDS,
  currentShortcutPlatform,
  defaultShortcutBindings,
  deleteShortcutPlatformOverride,
  findShortcutConflicts,
  isShortcutActionEnabled,
  macWindowsKeyPositionNotes,
  readKeyboardShortcutPreferences,
  readMacWindowsKeyPositions,
  resolveShortcutBindings,
  resetShortcutPreference,
  shortcutAriaKeys,
  shortcutBindingLabels,
  shortcutChordFromEvent,
  setShortcutRecordingActive,
  writeMacWindowsKeyPositions,
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
  { id: "questionCard", label: "Question cards", note: "card focused" },
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
      className="phone-hit-area relative flex min-h-7 items-center justify-end gap-1 rounded-md px-1.5 outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring phone:min-h-11 phone:min-w-11"
    >
      {recording ? (
        <span className="animate-pulse text-sm text-primary">Press shortcut…</span>
      ) : bindings.length === 0 ? (
        <span className="text-sm text-muted-foreground">Not set</span>
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
  useKeyboardShortcutsVersion();
  const [recording, setRecording] = useState<RecordingTarget | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const preferences = readKeyboardShortcutPreferences();
  const defaultContext = useMemo(
    () => ({
      submitWithModEnter: readSubmitWithModEnter(),
      nativeShell: isNativeShell(),
    }),
    [],
  );
  const defaultsFor = (
    actionId: ShortcutActionId,
    platform: ShortcutPlatform = currentShortcutPlatform(),
  ) => defaultShortcutBindings(actionId, defaultContext, platform);

  const stopRecording = () => {
    setShortcutRecordingActive(false);
    setRecording(null);
  };
  const normalizeSearch = (value: string) =>
    value
      .toLowerCase()
      .replace(/⌘|command|meta/g, "cmd")
      .replace(/⌃|control/g, "ctrl")
      .replace(/⇧/g, "shift")
      .replace(/⌥|option/g, "alt")
      .replace(/[\s+]+/g, "");
  const query = normalizeSearch(search.trim());
  const isChanged = (actionId: ShortcutActionId) => {
    const preference = preferences.actions[actionId];
    return (
      preference?.enabled === false ||
      preference?.common !== undefined ||
      Object.keys(preference?.platformOverrides ?? {}).length > 0
    );
  };
  const matches = (actionId: ShortcutActionId) => {
    if (changedOnly && !isChanged(actionId)) return false;
    const definition = DEFAULT_SHORTCUT_DEFINITIONS[actionId];
    return (
      !query ||
      [
        definition.label,
        definition.note ?? "",
        ...PLATFORMS.flatMap(({ id }) =>
          resolveShortcutBindings(actionId, id, defaultContext).flatMap((binding) => [
            shortcutBindingLabels(binding, id).join(" "),
            shortcutAriaKeys(binding, id),
          ]),
        ),
      ].some((value) => normalizeSearch(value).includes(query))
    );
  };

  const startRecording = (target: RecordingTarget) => {
    setError(null);
    setShortcutRecordingActive(true);
    setRecording(target);
  };

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
      if (recording.actionId === "recentSessions") {
        const holdsModifier = (["control", "meta", "alt"] as const).some((modifier) =>
          recordedChord.modifiers.includes(modifier),
        );
        const refusal = recordedChord.modifiers.includes("shift")
          ? "Switch recent sessions uses Shift to go back."
          : !holdsModifier
            ? "Switch recent sessions needs a modifier to hold."
            : null;
        if (refusal) {
          event.preventDefault();
          event.stopPropagation();
          setError(refusal);
          setShortcutRecordingActive(false);
          setRecording(null);
          return;
        }
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
        [platform]: current.common ?? defaultsFor(actionId, platform),
      },
    });
    setError(null);
  };

  const resetPlatform = (actionId: ShortcutActionId, platform: ShortcutPlatform) => {
    const current = readKeyboardShortcutPreferences().actions[actionId] ?? {};
    writeShortcutPreference(actionId, {
      ...current,
      platformOverrides: {
        ...current.platformOverrides,
        [platform]: defaultsFor(actionId, platform),
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
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Input
          aria-label="Search shortcuts"
          onFocus={stopRecording}
          placeholder="Search actions or keys"
          value={search}
          className="h-9 min-w-0 flex-1"
          onChange={(event) => {
            stopRecording();
            setSearch(event.target.value);
          }}
        />
        <label className="flex items-center gap-2 text-ui">
          <Switch
            aria-label="Changed only"
            checked={changedOnly}
            onCheckedChange={(value) => {
              stopRecording();
              setChangedOnly(value);
            }}
          />
          Changed only
        </label>
      </div>
      <p className="mb-4 text-sm text-muted-foreground">Click a shortcut to record.</p>
      {error ? (
        <p
          role="alert"
          className="mb-3 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      ) : null}
      {currentShortcutPlatform() === "macos" ? (
        <div className="mb-4 rounded-xl border border-border bg-card px-3 py-2">
          <SettingRow
            label="Use Windows key positions"
            hint="Default shortcuts use ⌘ where Windows uses Alt, and ⌃ where Windows uses Ctrl. Shortcuts you record yourself stay as recorded."
          >
            <Switch
              checked={readMacWindowsKeyPositions()}
              onCheckedChange={(checked) => writeMacWindowsKeyPositions(checked)}
              aria-label="Use Windows key positions"
            />
          </SettingRow>
        </div>
      ) : null}
      {GROUPS.filter((group) =>
        (definitionsByGroup.get(group.id) ?? []).some((definition) => matches(definition.id)),
      ).map((group) => (
        <details
          key={group.id}
          open={query || changedOnly ? true : undefined}
          className="mb-2 rounded-xl border border-border bg-card px-3 py-2 last:mb-0"
          onToggle={(event) => {
            if (
              !event.currentTarget.open &&
              recording &&
              DEFAULT_SHORTCUT_DEFINITIONS[recording.actionId].group === group.id
            )
              stopRecording();
          }}
        >
          <summary className="cursor-pointer select-none text-ui font-medium">
            {group.label}
            {group.note ? (
              <span className="ml-1.5 font-normal text-muted-foreground/70">· {group.note}</span>
            ) : null}
          </summary>
          <ul className="mt-1">
            {(definitionsByGroup.get(group.id) ?? [])
              .filter((definition) => matches(definition.id))
              .map((definition) => {
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
                    className="border-b border-border/60 py-1.5 last:border-b-0 last:pb-0"
                  >
                    <div className="flex flex-wrap items-center gap-2 phone:min-h-11 phone:gap-5">
                      <span className="min-w-32 flex-1">
                        <span className="block text-ui text-foreground">{definition.label}</span>
                        {definition.note ? (
                          <span className="block text-xs text-muted-foreground">
                            {definition.note}
                          </span>
                        ) : null}
                        {!actionPreference?.common && !platformOverrides.macos
                          ? macWindowsKeyPositionNotes(definition.id, defaultContext).map(
                              (note) => (
                                <span key={note} className="block text-xs text-muted-foreground">
                                  {note}
                                </span>
                              ),
                            )
                          : null}
                      </span>
                      <Switch
                        checked={enabled}
                        onCheckedChange={(checked) => setEnabled(definition.id, checked)}
                        aria-label={`${enabled ? "Disable" : "Enable"} ${definition.label} shortcut`}
                        className="scale-90 phone:after:min-h-[calc(44px/0.9)] phone:after:min-w-[calc(44px/0.9)]"
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
                        size="sm"
                        aria-label={`Restore default shortcut for ${definition.label}`}
                        disabled={!isChanged(definition.id)}
                        onClick={() => {
                          stopRecording();
                          resetShortcutPreference(definition.id);
                          setError(null);
                        }}
                      >
                        <RotateCcwIcon />
                        Reset
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
                    {PLATFORMS.filter(
                      (platform) => platformOverrides[platform.id] !== undefined,
                    ).map((platform) => (
                      <div
                        key={platform.id}
                        data-testid={`shortcut-platform-${definition.id}-${platform.id}`}
                        className="mt-1 ml-4 flex flex-wrap items-center gap-2 border-l border-border/60 pl-3 phone:mt-3 phone:min-h-11 phone:gap-5"
                      >
                        <span className="min-w-20 flex-1 text-sm text-muted-foreground">
                          {platform.label}
                        </span>
                        <ShortcutBindingButton
                          actionId={definition.id}
                          platform={platform.id}
                          bindings={
                            platformOverrides[platform.id] ??
                            defaultsFor(definition.id, platform.id)
                          }
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
                    ))}
                    {definition.id === "pollSessions" && <SessionNavigationSettings />}
                  </li>
                );
              })}
          </ul>
        </details>
      ))}
      {!SHORTCUT_ACTION_IDS.some(matches) && (
        <p role="status" className="py-3 text-sm text-muted-foreground">
          No matching shortcuts.
        </p>
      )}
      <span className="sr-only">
        Current platform: {platformLabel(currentShortcutPlatform())}. Effective shortcuts update
        immediately.
      </span>
    </div>
  );
}
