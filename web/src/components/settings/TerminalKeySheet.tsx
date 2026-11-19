import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { CircleIcon, SquareIcon, XIcon } from "lucide-react";

import { TerminalKeyContent } from "@/components/blocks/TerminalKeyContent";
import { Button } from "@/components/ui/button";
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
import { Textarea } from "@/components/ui/textarea";
import { useIsPhone } from "@/hooks/useIsPhone";
import {
  currentShortcutPlatform,
  setShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";
import { escapeForDisplay, escapeLiteral, unescapeFromDisplay } from "@/lib/keySequence";
import { mobileAssistantBindingLabel } from "@/lib/mobileAssistantPreferences";
import { MAX_KEY_LABEL_LENGTH } from "@/lib/terminalKeyBar";
import { recordTerminalKey } from "@/lib/terminalKeyRecording";
import { readTerminalKeyboardPreferences } from "@/lib/terminalKeyboardPreferences";
import {
  applyCmd,
  availableAction,
  CMD_KEY_MODE,
  keySupportsRepeat,
  MAX_SEND_BYTES,
  specialModifier,
  TERMINAL_CLIPBOARD_KEYS,
  type BarChord,
  type ClipboardOp,
  type KeyAction,
  type SpecialId,
  type TerminalKeyRowV2,
} from "@/lib/terminalKeys";
import { cn } from "@/lib/utils";
import { TerminalKeyIconPicker } from "./TerminalKeyIconPicker";

type StoredKey = TerminalKeyRowV2["keys"][number];
type FunctionKind = "send" | "special" | "keyboard" | "clipboard";

const SPECIAL_LABELS: Record<SpecialId, string> = {
  ctrl: "Ctrl",
  shift: "Shift",
  alt: "Alt",
  opt: "Opt",
  cmd: "Cmd",
  win: "Win",
  tab: "Tab",
  esc: "Esc",
};
const CLIPBOARD_LABELS: Record<ClipboardOp, string> = {
  copy: "Copy",
  paste: "Paste",
  pasteHost: "Paste from host",
};
const selectClass = "min-h-11 w-full rounded-md border border-input bg-background px-2 text-ui";

function comboLabel(chord: BarChord): string {
  const arrowKeys: Record<string, string> = {
    ArrowLeft: "←",
    ArrowRight: "→",
    ArrowUp: "↑",
    ArrowDown: "↓",
    Backspace: "⌫",
    Enter: "↵",
  };
  const key =
    arrowKeys[chord.code] ??
    mobileAssistantBindingLabel({ kind: "key", chord: { ...chord, modifiers: [] } });
  return `${chord.modifiers.includes("control") ? "⌃" : ""}${chord.modifiers.includes("alt") ? "⌥" : ""}${chord.modifiers.includes("shift") ? "⇧" : ""}⌘${key.length === 1 ? key.toUpperCase() : key}`;
}

function comboDescription(chord: BarChord): string {
  const result = applyCmd({ chord }, CMD_KEY_MODE);
  if ("clipboard" in result) return CLIPBOARD_LABELS[result.clipboard];
  if ("refused" in result) return result.refused;
  if (chord.modifiers.length === 1 && (CMD_KEY_MODE === "mac-meta" || CMD_KEY_MODE === "mac")) {
    if (chord.code === "ArrowLeft") return "Move to start of line";
    if (chord.code === "ArrowRight") return "Move to end of line";
    if (chord.code === "Backspace") return "Delete to start of line";
    if (chord.code === "Enter") return "Send Enter";
  }
  return `Sends ${escapeForDisplay(result.bytes)}`;
}

function legacySend(action: Extract<KeyAction, { type: "legacy" }>): KeyAction {
  const key = action.key;
  return key.kind === "text"
    ? { type: "send", send: escapeLiteral(key.text), ...(key.submit ? { autoEnter: true } : {}) }
    : { type: "send", send: "" };
}

function newClipboardAction(op: ClipboardOp): KeyAction {
  return op === "copy" ? { type: "clipboard", op } : { type: "clipboard", op, autoEnter: true };
}

function KeyPreview({ draft }: { draft: StoredKey }) {
  const previewRef = useRef<HTMLDivElement>(null);
  const [clipped, setClipped] = useState(false);

  useLayoutEffect(() => {
    const label = previewRef.current?.querySelector("span");
    const measure = () => setClipped(!!label && label.scrollWidth > label.clientWidth);
    measure();
    if (!label || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(label);
    return () => observer.disconnect();
  }, [draft.display, draft.icon, draft.label, draft.width]);

  return (
    <div className="flex min-w-0 items-center gap-2">
      <div
        ref={previewRef}
        data-testid="terminal-key-preview"
        aria-label="Key preview"
        className={cn(
          "flex h-11 shrink-0 items-center justify-center overflow-hidden rounded-[7px] border border-border bg-background px-1 font-mono text-xs font-semibold",
          draft.style === "danger" && "text-destructive",
        )}
        style={{ width: draft.width === 1 ? 28 : 59 }}
      >
        <TerminalKeyContent button={draft} />
      </div>
      {clipped ? <span className="text-xs text-muted-foreground">Label is clipped</span> : null}
    </div>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3 text-ui">
      {label}
      <Switch checked={checked} onCheckedChange={onChange} aria-label={label} />
    </label>
  );
}

export interface TerminalKeySheetProps {
  keyDraft: StoredKey;
  isNew: boolean;
  error?: string | null;
  onClose: () => void;
  onSave: (key: StoredKey) => void;
  onDelete: (id: string) => void;
  onMove: (id: string, direction: "left" | "right" | "other-row") => void;
}

export function TerminalKeySheet({
  keyDraft,
  isNew,
  error,
  onClose,
  onSave,
  onDelete,
  onMove,
}: TerminalKeySheetProps) {
  const isPhone = useIsPhone();
  const [draft, setDraft] = useState(keyDraft);
  const [labelEdited, setLabelEdited] = useState(!isNew && !!keyDraft.label);
  const [recording, setRecording] = useState(false);
  const [recordMessage, setRecordMessage] = useState("");
  const sink = useRef<HTMLSpanElement>(null);
  const recordButton = useRef<HTMLButtonElement>(null);
  const sendHintId = useId();
  const action = availableAction(draft);
  const currentKind = action?.type === "combo" ? "send" : action?.type;
  const clipboardOps = [...TERMINAL_CLIPBOARD_KEYS];
  const showSend = currentKind === "send" || action?.type === "legacy";
  const supportsRepeat = keySupportsRepeat(draft);
  const initialAction = availableAction(keyDraft);
  const send = action?.type === "send" ? action.send : "";
  const decoded = unescapeFromDisplay(send);
  const sendBytes = new TextEncoder().encode(decoded).length;
  const originalBytes =
    initialAction?.type === "send"
      ? new TextEncoder().encode(unescapeFromDisplay(initialAction.send)).length
      : 0;
  const sendTooLong = sendBytes > MAX_SEND_BYTES && sendBytes > originalBytes;
  const labelTooLong =
    draft.label.length > MAX_KEY_LABEL_LENGTH && draft.label.length > keyDraft.label.length;

  useEffect(() => {
    if (!recording) return;
    const platform = currentShortcutPlatform();
    const preferences = readTerminalKeyboardPreferences();
    const onKeyDown = (event: KeyboardEvent) => {
      const outcome = recordTerminalKey(event, preferences, platform);
      event.stopImmediatePropagation();
      if (!outcome) return;
      event.preventDefault();
      if ("refused" in outcome) {
        setRecordMessage(
          outcome.refused === "altgr"
            ? "AltGr types characters. Type the character into the field."
            : "This key has no terminal sequence.",
        );
        return;
      }
      setDraft((current) => {
        const next = { ...current, action: outcome.action };
        if (!keySupportsRepeat(next)) delete next.repeat;
        if (!labelEdited || !current.label.trim()) {
          const label =
            outcome.action.type === "combo" ? comboLabel(outcome.action.chord) : outcome.label;
          next.label = label.slice(0, MAX_KEY_LABEL_LENGTH);
        }
        return next;
      });
      setRecordMessage("");
      setRecording(false);
      recordButton.current?.focus({ preventScroll: true });
    };
    const stopOnBlur = () => {
      setRecording(false);
      setRecordMessage("");
      sink.current?.blur();
    };
    setShortcutRecordingActive(true);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("blur", stopOnBlur);
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    sink.current?.focus({ preventScroll: true });
    return () => {
      setShortcutRecordingActive(false);
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("blur", stopOnBlur);
    };
  }, [recording, labelEdited]);

  const changeAction = (nextAction: KeyAction, label?: string) => {
    setDraft((current) => {
      const next = { ...current, action: nextAction };
      if (!keySupportsRepeat(next)) delete next.repeat;
      if (label !== undefined && !labelEdited) next.label = label.slice(0, MAX_KEY_LABEL_LENGTH);
      return next;
    });
  };

  const chooseKind = (kind: FunctionKind) => {
    setRecording(false);
    setRecordMessage("");
    if (kind === currentKind) return;
    if (kind === "send") {
      changeAction(action?.type === "legacy" ? legacySend(action) : { type: "send", send: "" });
    } else if (kind === "special") {
      changeAction({ type: "special", special: "ctrl" }, "Ctrl");
    } else if (kind === "keyboard") {
      changeAction({ type: "keyboard" }, "Keyboard");
    } else if (clipboardOps.length) {
      changeAction(newClipboardAction(clipboardOps[0]), CLIPBOARD_LABELS[clipboardOps[0]]);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="flex max-h-[88vh] flex-col overflow-hidden sm:max-w-md phone:p-3"
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
          <DialogDescription className="sr-only">
            Set the key’s function and appearance.
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-4 overflow-y-auto p-1">
          <div className="flex items-center justify-between gap-3">
            <KeyPreview draft={draft} />
            <Segmented
              label="Width"
              value={draft.width === 1 ? "1" : "2"}
              onChange={(width) => setDraft({ ...draft, width: width === "1" ? 1 : 2 })}
              options={[
                { value: "1", label: "1 cell" },
                { value: "2", label: "2 cells" },
              ]}
            />
          </div>

          <div className="grid gap-2">
            {action?.type === "legacy" ? (
              <p className="text-sm text-muted-foreground">
                Kept from the previous editor: {mobileAssistantBindingLabel(action.key)}
              </p>
            ) : !action ? (
              <p className="text-sm text-muted-foreground">
                {draft.action.type === "clipboard" ? "Not available" : "Needs a newer version"}
              </p>
            ) : null}
            <label className="grid gap-1.5 text-ui">
              Function
              <select
                className={selectClass}
                value={action && action.type !== "legacy" ? currentKind : ""}
                onChange={(event) => chooseKind(event.target.value as FunctionKind)}
              >
                {!action || action.type === "legacy" ? (
                  <option value="" disabled>
                    Change function…
                  </option>
                ) : null}
                <option value="send">Send to terminal</option>
                <option value="special">Special key</option>
                <option value="keyboard">Keyboard toggle</option>
                {clipboardOps.length ? <option value="clipboard">Clipboard</option> : null}
              </select>
            </label>
          </div>

          {showSend ? (
            <div className="grid gap-2">
              <p className="text-ui">Send sequence</p>
              <div className="flex items-center gap-2">
                {action?.type === "combo" ? (
                  <div
                    className="flex min-h-11 min-w-0 flex-1 items-center gap-1 rounded-md border border-input px-2"
                    aria-label="Recorded shortcut"
                  >
                    <span className="rounded bg-muted px-2 py-1 font-mono text-sm">
                      {comboLabel(action.chord)}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-11"
                      aria-label="Clear recorded shortcut"
                      onClick={() => changeAction({ type: "send", send: "" })}
                    >
                      <XIcon aria-hidden />
                    </Button>
                  </div>
                ) : (
                  <Textarea
                    aria-label="Send sequence"
                    aria-describedby={sendHintId}
                    aria-invalid={sendTooLong || undefined}
                    placeholder="Commands or text; use Record for key chords"
                    className="min-w-0 flex-1 font-mono"
                    rows={2}
                    value={
                      action?.type === "legacy" && action.key.kind === "text"
                        ? escapeLiteral(action.key.text)
                        : send
                    }
                    onChange={(event) => {
                      const value = event.target.value;
                      const autoEnter =
                        action?.type === "send"
                          ? action.autoEnter
                          : action?.type === "legacy" &&
                            action.key.kind === "text" &&
                            action.key.submit;
                      changeAction({
                        type: "send",
                        send: value,
                        ...(autoEnter ? { autoEnter: true } : {}),
                      });
                    }}
                  />
                )}
                <Button
                  ref={recordButton}
                  type="button"
                  variant="outline"
                  className="min-h-11 shrink-0"
                  aria-pressed={recording}
                  onClick={() => {
                    setRecordMessage("");
                    setRecording(!recording);
                  }}
                >
                  {recording ? <SquareIcon aria-hidden /> : <CircleIcon aria-hidden />}
                  {recording ? "Stop" : "Record"}
                </Button>
                <span ref={sink} className="sr-only" tabIndex={-1}>
                  Terminal key recording
                </span>
              </div>
              <div
                id={sendHintId}
                className="grid gap-1 text-sm text-muted-foreground"
                role="status"
              >
                {action?.type === "combo" ? (
                  <span>
                    {comboLabel(action.chord)} → {comboDescription(action.chord)}
                  </span>
                ) : null}
                {send ? (
                  <span className="wrap-anywhere">
                    Sends <code className="whitespace-pre-wrap">{escapeForDisplay(decoded)}</code>
                    {` · ${sendBytes} ${sendBytes === 1 ? "byte" : "bytes"}`}
                  </span>
                ) : null}
                {sendTooLong ? (
                  <span className="text-destructive">
                    Decoded sequence is {sendBytes} bytes. The limit is {MAX_SEND_BYTES} bytes.
                  </span>
                ) : null}
                {recording ? (
                  <span>
                    {recordMessage ||
                      "Recording. Press a key or combination. macOS can reserve ⌘N, ⌘W, ⌘Q and ⌘T. Windows can reserve Win shortcuts."}
                  </span>
                ) : null}
              </div>
              {action?.type === "send" ? (
                <ToggleRow
                  label="Send Enter after"
                  checked={action.autoEnter === true}
                  onChange={(checked) =>
                    changeAction({ type: "send", send, ...(checked ? { autoEnter: true } : {}) })
                  }
                />
              ) : null}
            </div>
          ) : null}

          {action?.type === "special" ? (
            <div className="grid gap-1.5">
              <label className="grid gap-1.5 text-ui">
                Special key
                <select
                  className={selectClass}
                  value={action.special}
                  onChange={(event) => {
                    const special = event.target.value as SpecialId;
                    changeAction(
                      {
                        type: "special",
                        special,
                        ...(special !== "tab" && special !== "esc" && action.keepHeld
                          ? { keepHeld: true }
                          : {}),
                      },
                      SPECIAL_LABELS[special],
                    );
                  }}
                >
                  {Object.entries(SPECIAL_LABELS)
                    .filter(([id]) => CMD_KEY_MODE !== "none" || (id !== "cmd" && id !== "win"))
                    .map(([id, label]) => (
                      <option key={id} value={id}>
                        {label}
                      </option>
                    ))}
                </select>
              </label>
              {specialModifier(draft) ? (
                <ToggleRow
                  label="Keep held until tapped again"
                  checked={action.keepHeld === true}
                  onChange={(checked) =>
                    changeAction({
                      type: "special",
                      special: action.special,
                      ...(checked ? { keepHeld: true } : {}),
                    })
                  }
                />
              ) : null}
              {action.special === "cmd" || action.special === "win" ? (
                <p className="text-sm text-muted-foreground">
                  {CMD_KEY_MODE === "meta"
                    ? "Acts as terminal Meta (Escape prefix)."
                    : "Uses Mac terminal shortcuts for ←, →, ⌫, Enter, C and V."}
                  {CMD_KEY_MODE === "mac-meta"
                    ? " Other combinations act as terminal Meta (Escape prefix)."
                    : null}
                </p>
              ) : null}
            </div>
          ) : null}

          {action?.type === "clipboard" ? (
            <div className="grid gap-1.5">
              <label className="grid gap-1.5 text-ui">
                Clipboard action
                <select
                  className={selectClass}
                  value={action.op}
                  onChange={(event) => {
                    const op = event.target.value as ClipboardOp;
                    changeAction(newClipboardAction(op), CLIPBOARD_LABELS[op]);
                  }}
                >
                  {clipboardOps.map((op) => (
                    <option key={op} value={op}>
                      {CLIPBOARD_LABELS[op]}
                    </option>
                  ))}
                </select>
              </label>
              {action.op !== "copy" ? (
                <ToggleRow
                  label="Press Enter after one line"
                  checked={action.autoEnter === true}
                  onChange={(checked) => changeAction({ ...action, autoEnter: checked })}
                />
              ) : null}
            </div>
          ) : null}
          {supportsRepeat ? (
            <ToggleRow
              label="Repeat on hold"
              checked={draft.repeat === true}
              onChange={(checked) => {
                const { repeat: _repeat, ...base } = draft;
                setDraft(checked ? { ...base, repeat: true } : base);
              }}
            />
          ) : null}

          <label className="grid gap-1.5 text-ui">
            Label
            <Input
              className="min-h-11"
              value={draft.label}
              maxLength={Math.max(keyDraft.label.length, MAX_KEY_LABEL_LENGTH)}
              aria-invalid={labelTooLong || undefined}
              onChange={(event) => {
                setLabelEdited(true);
                setDraft({ ...draft, label: event.target.value });
              }}
            />
            {labelTooLong ? (
              <span className="text-sm text-destructive">
                Use at most {MAX_KEY_LABEL_LENGTH} characters.
              </span>
            ) : null}
          </label>
          <div className="grid justify-items-start gap-2">
            <Segmented
              label="Display"
              value={draft.display}
              onChange={(display) =>
                setDraft({
                  ...draft,
                  display,
                  ...(display === "icon" && !draft.icon ? { icon: "square-terminal" } : {}),
                })
              }
              options={[
                { value: "text", label: "Text" },
                { value: "icon", label: "Icon" },
              ]}
            />
            {draft.display === "icon" ? (
              <div className="w-full">
                <TerminalKeyIconPicker
                  value={draft.icon ?? ""}
                  onChange={(icon) => setDraft({ ...draft, icon })}
                />
              </div>
            ) : null}
          </div>
          <div>
            <Segmented
              label="Style"
              value={draft.style ?? "normal"}
              onChange={(style) => {
                const { style: _style, ...base } = draft;
                setDraft(style === "danger" ? { ...base, style } : base);
              }}
              options={[
                { value: "normal", label: "Normal" },
                { value: "danger", label: "Danger" },
              ]}
            />
          </div>

          {!isNew ? (
            <div className="flex flex-wrap gap-2" role="group" aria-label="Move key">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => onMove(draft.id, "left")}
              >
                Move left
              </Button>
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => onMove(draft.id, "right")}
              >
                Move right
              </Button>
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => onMove(draft.id, "other-row")}
              >
                Move to other row
              </Button>
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
                className="min-h-11 text-destructive"
                onClick={() => onDelete(draft.id)}
              >
                Delete key
              </Button>
            ) : null}
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" className="min-h-11" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              className="min-h-11"
              disabled={
                recording ||
                !draft.label.trim() ||
                labelTooLong ||
                sendTooLong ||
                (action?.type === "send" && !decoded)
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
