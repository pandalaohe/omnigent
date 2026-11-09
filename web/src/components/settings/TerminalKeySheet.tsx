import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArchiveIcon,
  ArrowDownIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  ChevronsDownIcon,
  ChevronsUpIcon,
  CommandIcon,
  CornerDownLeftIcon,
  DeleteIcon,
  KeyboardIcon,
  RefreshCwIcon,
  SquareTerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useIsPhone } from "@/hooks/useIsPhone";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import type { ShortcutChord } from "@/lib/keyboardShortcutPreferences";
import {
  MAX_KEY_LABEL_LENGTH,
  MAX_KEY_TEXT_LENGTH,
  TERMINAL_KEY_ICONS,
  type TerminalBarKey,
  type TerminalKeyIcon,
  type TerminalKeyModifier,
} from "@/lib/terminalKeyBar";
import {
  mobileAssistantBindingLabel,
  softKeyForChord,
  terminalDataForChord,
} from "@/lib/mobileAssistantPreferences";

type FunctionKind = "key" | "text" | "modifier" | "keyboard";

const FUNCTION_KIND: Record<TerminalBarKey["kind"], FunctionKind> = {
  key: "key",
  text: "text",
  modifier: "modifier",
  action: "keyboard",
};

const KEY_GROUPS: readonly { label: string; choices: readonly [string, string][] }[] = [
  {
    label: "Common",
    choices: [
      ["Escape", "Esc"],
      ["Tab", "Tab"],
      ["Enter", "Enter"],
      ["Space", "Space"],
      ["Backspace", "Backspace"],
      ["Delete", "Delete"],
    ],
  },
  {
    label: "Arrows and navigation",
    choices: [
      ["ArrowUp", "↑"],
      ["ArrowDown", "↓"],
      ["ArrowLeft", "←"],
      ["ArrowRight", "→"],
      ["Home", "Home"],
      ["End", "End"],
      ["PageUp", "PgUp"],
      ["PageDown", "PgDn"],
      ["Insert", "Insert"],
    ],
  },
  {
    label: "Letters",
    choices: Array.from("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (letter) => [`Key${letter}`, letter]),
  },
  {
    label: "Digits",
    choices: Array.from("0123456789", (digit) => [`Digit${digit}`, digit]),
  },
  {
    label: "Symbols",
    choices: [
      ["Slash", "/"],
      ["Backslash", "\\"],
      ["BracketLeft", "["],
      ["BracketRight", "]"],
      ["Minus", "-"],
      ["Equal", "="],
      ["Semicolon", ";"],
      ["Quote", "'"],
      ["Comma", ","],
      ["Period", "."],
      ["Backquote", "`"],
    ],
  },
  {
    label: "Function keys",
    choices: Array.from({ length: 12 }, (_, index) => [`F${index + 1}`, `F${index + 1}`]) as [
      string,
      string,
    ][],
  },
];

const ICON_COMPONENTS: Record<TerminalKeyIcon, LucideIcon> = {
  escape: XIcon,
  tab: ArrowRightIcon,
  "arrow-up": ArrowUpIcon,
  "arrow-down": ArrowDownIcon,
  "arrow-left": ArrowLeftIcon,
  "arrow-right": ArrowRightIcon,
  enter: CornerDownLeftIcon,
  backspace: DeleteIcon,
  delete: DeleteIcon,
  "page-up": ChevronsUpIcon,
  "page-down": ChevronsDownIcon,
  keyboard: KeyboardIcon,
  refresh: RefreshCwIcon,
  archive: ArchiveIcon,
  terminal: SquareTerminalIcon,
  command: CommandIcon,
};

function chordLabel(chord: ShortcutChord): string {
  let label = mobileAssistantBindingLabel({ kind: "key", chord }).replace("Control", "Ctrl");
  if (/^Key[A-Z]$/.test(chord.code)) label = `${label.slice(0, -1)}${chord.code.slice(3)}`;
  return label.length <= MAX_KEY_LABEL_LENGTH
    ? label
    : label.replace("Ctrl+", "⌃").replace("Alt+", "⌥").replace("Shift+", "⇧");
}

function automaticLabel(key: TerminalBarKey): string {
  if (key.kind === "key") return chordLabel(key.chord);
  if (key.kind === "text") return key.text;
  if (key.kind === "modifier")
    return key.modifier === "ctrl" ? "Ctrl" : key.modifier === "alt" ? "Alt" : "Shift";
  return "Keyboard";
}

function withFunction(key: TerminalBarKey, kind: FunctionKind): TerminalBarKey {
  const base = {
    id: key.id,
    width: key.width,
    display: key.display,
    ...(key.icon ? { icon: key.icon } : {}),
  };
  if (kind === "text")
    return {
      ...base,
      kind: "text",
      text: key.kind === "text" ? key.text : "ok",
      submit: key.kind === "text" ? key.submit : true,
      label: key.kind === "text" ? key.label : "ok",
    };
  if (kind === "modifier")
    return {
      ...base,
      kind: "modifier",
      modifier: key.kind === "modifier" ? key.modifier : "ctrl",
      tapLocks: key.kind === "modifier" ? key.tapLocks : false,
      label: key.kind === "modifier" ? key.label : "Ctrl",
    };
  if (kind === "keyboard")
    return { ...base, kind: "action", action: "keyboard", label: "Keyboard" };
  return {
    ...base,
    kind: "key",
    chord: key.kind === "key" ? key.chord : { code: "Escape", modifiers: [] },
    repeat: key.kind === "key" ? key.repeat : false,
    label: key.kind === "key" ? key.label : "Esc",
  };
}

function KeyPreview({ barKey, onCut }: { barKey: TerminalBarKey; onCut: (cut: boolean) => void }) {
  const labelRef = useRef<HTMLSpanElement>(null);
  const Icon = barKey.display === "icon" && barKey.icon ? ICON_COMPONENTS[barKey.icon] : null;
  useLayoutEffect(() => {
    const label = labelRef.current;
    const measure = () => onCut(Boolean(label && label.scrollWidth > label.clientWidth));
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (label) observer?.observe(label);
    return () => observer?.disconnect();
  }, [barKey.label, barKey.width, barKey.display, onCut]);
  return (
    <div
      data-testid="terminal-key-preview"
      className="flex h-11 items-center justify-center rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground"
      style={{ width: barKey.width === 1 ? 28 : 59 }}
    >
      {Icon ? (
        <Icon className="size-3.5" aria-hidden />
      ) : (
        <span
          ref={labelRef}
          className={Array.from(barKey.label).length === 1 ? "shrink-0" : "truncate"}
        >
          {barKey.label}
        </span>
      )}
    </div>
  );
}

export interface TerminalKeySheetProps {
  open: boolean;
  keyDraft: TerminalBarKey | null;
  isNew: boolean;
  error?: string | null;
  onClose: () => void;
  onSave: (key: TerminalBarKey) => void;
  onDelete: (id: string) => void;
  onMove: (id: string, direction: "left" | "right" | "other-row") => void;
}

export function TerminalKeySheet({
  open,
  keyDraft,
  isNew,
  error,
  onClose,
  onSave,
  onDelete,
  onMove,
}: TerminalKeySheetProps) {
  const isPhone = useIsPhone();
  const [draft, setDraft] = useState<TerminalBarKey | null>(keyDraft);
  const [labelEdited, setLabelEdited] = useState(false);
  const [wouldCut, setWouldCut] = useState(false);

  useEffect(() => {
    setDraft(keyDraft);
    setLabelEdited(Boolean(!isNew && keyDraft && keyDraft.label !== automaticLabel(keyDraft)));
  }, [keyDraft, isNew]);

  if (!draft) return null;
  const kind = FUNCTION_KIND[draft.kind];
  const unsupportedChord =
    draft.kind === "key" &&
    softKeyForChord(draft.chord) === null &&
    terminalDataForChord(draft.chord) === null;

  const setKey = (next: TerminalBarKey) => setDraft(next);
  const chooseCode = (code: string) => {
    if (draft.kind !== "key") return;
    const chord = { ...draft.chord, code };
    setKey({
      ...draft,
      chord,
      ...(isNew
        ? {
            repeat: [
              "ArrowUp",
              "ArrowDown",
              "ArrowLeft",
              "ArrowRight",
              "Backspace",
              "Delete",
              "PageUp",
              "PageDown",
            ].includes(code),
          }
        : {}),
      ...(labelEdited ? {} : { label: chordLabel(chord) }),
    });
  };
  const updateLabel = (label: string) => {
    setLabelEdited(true);
    setKey({ ...draft, label });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent
        className="flex max-h-[88vh] flex-col overflow-hidden sm:max-w-md"
        style={
          isPhone
            ? {
                top: "auto",
                left: 0,
                bottom: "calc(100lvh - var(--omnigent-viewport-height, 100lvh))",
                transform: "none",
                translate: "none",
                width: "100%",
                maxWidth: "100%",
                borderBottomLeftRadius: 0,
                borderBottomRightRadius: 0,
                maxHeight:
                  "calc(var(--omnigent-viewport-height, 100dvh) - var(--omnigent-safe-top, 0px) - 8px)",
              }
            : undefined
        }
      >
        <DialogHeader className="shrink-0">
          <DialogTitle>{isNew ? "Add terminal key" : "Edit terminal key"}</DialogTitle>
          <DialogDescription>
            Choose what the key sends, how it looks, and how wide it is.
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-4 overflow-y-auto">
          <div className="flex items-center justify-between gap-3">
            <KeyPreview barKey={draft} onCut={setWouldCut} />
            <span className="text-sm text-muted-foreground">
              {draft.width === 1 ? "1 cell" : "2 cells"}
              {wouldCut
                ? ` · Cut off at ${draft.width} ${draft.width === 1 ? "cell" : "cells"}`
                : ""}
            </span>
          </div>
          <div className="grid gap-1.5 text-ui">
            <span>Function</span>
            <Segmented<FunctionKind>
              label="Function"
              value={kind}
              onChange={(next) => {
                const updated = withFunction(draft, next);
                setKey(labelEdited ? { ...updated, label: draft.label } : updated);
              }}
              options={[
                { value: "key", label: "Key" },
                { value: "text", label: "Text" },
                { value: "modifier", label: "Modifier" },
                { value: "keyboard", label: "Keyboard" },
              ]}
            />
          </div>
          <div className="grid gap-1.5 text-ui">
            <span>Show</span>
            <Segmented<"text" | "icon">
              label="Show"
              value={draft.display}
              onChange={(display) =>
                setKey({
                  ...draft,
                  display,
                  ...(display === "icon" && !draft.icon ? { icon: "command" as const } : {}),
                })
              }
              options={[
                { value: "text", label: "Text" },
                { value: "icon", label: "Icon" },
              ]}
            />
          </div>
          {draft.display === "icon" ? (
            <div className="grid gap-1.5 text-ui">
              <span>Icon</span>
              <div role="radiogroup" aria-label="Icon" className="grid grid-cols-5 gap-2">
                {TERMINAL_KEY_ICONS.map((icon) => {
                  const Icon = ICON_COMPONENTS[icon.id];
                  return (
                    <button
                      key={icon.id}
                      type="button"
                      role="radio"
                      aria-label={icon.label}
                      aria-checked={draft.icon === icon.id}
                      onClick={() => setKey({ ...draft, icon: icon.id })}
                      className={
                        draft.icon === icon.id
                          ? "flex h-11 items-center justify-center rounded-md border border-primary bg-primary/15"
                          : "flex h-11 items-center justify-center rounded-md border border-border"
                      }
                    >
                      <Icon className="size-4" aria-hidden />
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}
          <label className="grid gap-1.5 text-ui">
            Label
            <Input
              value={draft.label}
              maxLength={MAX_KEY_LABEL_LENGTH}
              onChange={(event) => updateLabel(event.target.value)}
            />
          </label>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="justify-self-start"
            onClick={() => {
              setLabelEdited(false);
              setKey({ ...draft, label: automaticLabel(draft) });
            }}
          >
            Reset to automatic
          </Button>

          {draft.kind === "key" ? (
            <>
              <div className="grid gap-1.5 text-ui">
                <div className="grid grid-cols-5 gap-1" role="group" aria-label="Common keys">
                  {KEY_GROUPS.slice(0, 2)
                    .flatMap((group) => group.choices)
                    .map(([code, label]) => (
                      <button
                        key={code}
                        type="button"
                        aria-label={label}
                        aria-pressed={draft.chord.code === code}
                        onClick={() => chooseCode(code)}
                        className="min-h-11 min-w-0 rounded-md border border-border px-1 text-xs aria-pressed:border-primary aria-pressed:bg-primary/15"
                      >
                        {code === "Backspace" ? (
                          <DeleteIcon className="mx-auto size-4" aria-hidden />
                        ) : (
                          label
                        )}
                      </button>
                    ))}
                </div>
                <label className="grid gap-1.5">
                  More keys
                  <select
                    aria-label="More keys"
                    className="min-h-11 rounded-md border border-input bg-background px-2"
                    value={draft.chord.code}
                    onChange={(event) => chooseCode(event.target.value)}
                  >
                    {KEY_GROUPS.map((group) => (
                      <optgroup key={group.label} label={group.label}>
                        {group.choices.map(([code, label]) => (
                          <option key={code} value={code}>
                            {label}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                    {!KEY_GROUPS.some((group) =>
                      group.choices.some(([code]) => code === draft.chord.code),
                    ) && <option value={draft.chord.code}>{draft.chord.code}</option>}
                  </select>
                </label>
                <div className="flex flex-wrap gap-2" role="group" aria-label="Key modifiers">
                  {(
                    [
                      ["control", "Ctrl"],
                      ["alt", "Alt"],
                      ["shift", "Shift"],
                    ] as const
                  ).map(([modifier, label]) => (
                    <Button
                      key={modifier}
                      type="button"
                      variant="outline"
                      aria-pressed={draft.chord.modifiers.includes(modifier)}
                      className={
                        draft.chord.modifiers.includes(modifier)
                          ? "bg-primary-cta text-primary-cta-foreground"
                          : undefined
                      }
                      onClick={() => {
                        const chord: ShortcutChord = {
                          ...draft.chord,
                          modifiers: draft.chord.modifiers.includes(modifier)
                            ? draft.chord.modifiers.filter((item) => item !== modifier)
                            : [...draft.chord.modifiers, modifier],
                        };
                        setKey({
                          ...draft,
                          chord,
                          ...(labelEdited ? {} : { label: chordLabel(chord) }),
                        });
                      }}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </div>
              {unsupportedChord ? (
                <p role="status" className="text-sm text-muted-foreground">
                  Not supported in terminals. Choose a different key or modifier.
                </p>
              ) : null}
              <label className="flex min-h-11 items-center justify-between gap-3 text-ui">
                Repeat while held
                <Switch
                  checked={draft.repeat === true}
                  onCheckedChange={(repeat) => setKey({ ...draft, repeat })}
                  aria-label="Repeat while held"
                />
              </label>
            </>
          ) : null}

          {draft.kind === "text" ? (
            <>
              <label className="grid gap-1.5 text-ui">
                Text
                <Input
                  value={draft.text}
                  maxLength={MAX_KEY_TEXT_LENGTH}
                  onChange={(event) => {
                    const text = event.target.value;
                    setKey({
                      ...draft,
                      text,
                      ...(labelEdited ? {} : { label: text }),
                    });
                  }}
                />
              </label>
              <label className="flex min-h-11 items-center justify-between gap-3 text-ui">
                Send Enter after
                <Switch
                  checked={draft.submit === true}
                  onCheckedChange={(submit) => setKey({ ...draft, submit })}
                  aria-label="Send Enter after"
                />
              </label>
            </>
          ) : null}

          {draft.kind === "modifier" ? (
            <>
              <label className="grid gap-1.5 text-ui">
                Modifier
                <select
                  aria-label="Modifier"
                  className="min-h-11 rounded-md border border-input bg-background px-2"
                  value={draft.modifier}
                  onChange={(event) => {
                    const modifier = event.target.value as TerminalKeyModifier;
                    setKey({
                      ...draft,
                      modifier,
                      ...(labelEdited
                        ? {}
                        : {
                            label:
                              modifier === "ctrl" ? "Ctrl" : modifier === "alt" ? "Alt" : "Shift",
                          }),
                    });
                  }}
                >
                  <option value="ctrl">Ctrl</option>
                  <option value="alt">Alt</option>
                  <option value="shift">Shift</option>
                </select>
              </label>
              <label className="flex min-h-11 items-center justify-between gap-3 text-ui">
                Tap locks
                <Switch
                  checked={draft.tapLocks === true}
                  onCheckedChange={(tapLocks) => setKey({ ...draft, tapLocks })}
                  aria-label="Tap locks"
                />
              </label>
              <p className="text-sm text-muted-foreground">
                {draft.tapLocks
                  ? "Tap locks · tap again releases"
                  : "Tap = next key only · hold = lock"}
              </p>
            </>
          ) : null}

          <div className="grid gap-1.5 text-ui">
            <span>Width</span>
            <Segmented<"1" | "2">
              label="Width"
              value={String(draft.width) as "1" | "2"}
              onChange={(width) => setKey({ ...draft, width: width === "1" ? 1 : 2 })}
              options={[
                { value: "1", label: "1 cell" },
                { value: "2", label: "2 cells" },
              ]}
            />
          </div>

          {!isNew ? (
            <div className="grid gap-1.5 text-ui">
              <span>Move</span>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onMove(draft.id, "left")}
                >
                  Move left
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onMove(draft.id, "right")}
                >
                  Move right
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onMove(draft.id, "other-row")}
                >
                  Move to other row
                </Button>
              </div>
            </div>
          ) : null}

          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter className="shrink-0 flex-row items-center justify-between gap-2 sm:justify-between">
          <div>
            {!isNew ? (
              <Button
                type="button"
                variant="ghost"
                className="text-destructive"
                onClick={() => onDelete(draft.id)}
              >
                Delete key
              </Button>
            ) : null}
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={
                !draft.label.trim() || unsupportedChord || (draft.kind === "text" && !draft.text)
              }
              onClick={() => onSave(draft)}
            >
              Save
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
