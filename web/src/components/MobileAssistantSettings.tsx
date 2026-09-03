import { useEffect, useReducer, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { KeyboardIcon, PencilIcon, PlusIcon, RotateCcwIcon, Trash2Icon } from "lucide-react";

import { Kbd } from "@/components/KeyboardShortcut";
import {
  MobileAssistantButtonContent,
  layoutMobileAssistantActions,
} from "@/components/MobileFloatingAssistant";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  SHORTCUT_ACTION_IDS,
  currentShortcutPlatform,
  setShortcutRecordingActive,
  shortcutBindingLabels,
  shortcutChordFromEvent,
  type ShortcutActionId,
  type ShortcutChord,
} from "@/lib/keyboardShortcutPreferences";
import {
  MOBILE_ASSISTANT_CHANGED_EVENT,
  MOBILE_ASSISTANT_ICONS,
  MOBILE_ASSISTANT_MAX_BUTTONS,
  defaultMobileAssistantButtons,
  mobileAssistantBindingLabel,
  mobileAssistantBindingSupportsRepeat,
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
  type MobileAssistantButton,
  type MobileAssistantButtonBinding,
  type MobileAssistantIcon,
} from "@/lib/mobileAssistantPreferences";

type BindingKind = MobileAssistantButtonBinding["kind"];

interface ButtonDraft {
  id: string;
  label: string;
  kind: BindingKind;
  actionId: ShortcutActionId;
  chord: ShortcutChord;
  text: string;
  submit: boolean;
  display: "text" | "icon";
  icon: MobileAssistantIcon;
  repeat: boolean;
}

function createId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `mobile-button-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function draftFromButton(button?: MobileAssistantButton): ButtonDraft {
  const binding = button?.binding;
  return {
    id: button?.id ?? createId(),
    label: button?.label ?? "New",
    kind: binding?.kind ?? "shortcut",
    actionId: binding?.kind === "shortcut" ? binding.actionId : "pollSessions",
    chord: binding?.kind === "key" ? binding.chord : { code: "KeyT", modifiers: ["primary"] },
    text: binding?.kind === "text" ? binding.text : "/compact",
    submit: binding?.kind === "text" ? binding.submit === true : false,
    display: button?.display === "icon" ? "icon" : "text",
    icon: button?.icon ?? "command",
    repeat: button?.repeat === true,
  };
}

function normalizeRecordedPrimary(chord: ShortcutChord): ShortcutChord {
  const platform = currentShortcutPlatform();
  const primary = platform === "macos" ? "meta" : "control";
  if (!chord.modifiers.includes(primary)) return chord;
  return {
    ...chord,
    modifiers: chord.modifiers.map((modifier) => (modifier === primary ? "primary" : modifier)),
  };
}

function bindingFromDraft(draft: ButtonDraft): MobileAssistantButtonBinding {
  if (draft.kind === "shortcut") return { kind: "shortcut", actionId: draft.actionId };
  if (draft.kind === "key") return { kind: "key", chord: draft.chord };
  return { kind: "text", text: draft.text, submit: draft.submit || undefined };
}

function bindingSummary(button: MobileAssistantButton): string {
  return mobileAssistantBindingLabel(button.binding);
}

function moveButtonToSlot(
  buttons: MobileAssistantButton[],
  id: string,
  targetIndex: number,
): MobileAssistantButton[] {
  const fromIndex = buttons.findIndex((button) => button.id === id);
  if (fromIndex < 0 || fromIndex === targetIndex) return buttons;
  const next = [...buttons];
  const [moving] = next.splice(fromIndex, 1);
  if (!moving) return buttons;
  next.splice(Math.max(0, Math.min(targetIndex, next.length)), 0, moving);
  return next;
}

function circleSlotFromPoint(
  clientX: number,
  clientY: number,
  circle: HTMLElement,
  count: number,
): number | null {
  if (count === 0) return null;
  const rect = circle.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const radius = Math.min(rect.width, rect.height) / 2;
  if (Math.hypot(clientX - centerX, clientY - centerY) > radius) return null;
  if (count === 1) return 0;

  const slots = layoutMobileAssistantActions(
    count,
    { x: rect.width / 2, y: rect.height / 2 },
    { width: rect.width, height: rect.height },
  );
  let nearestIndex = 0;
  let nearestDistance = Number.POSITIVE_INFINITY;
  slots.forEach((slot, index) => {
    const distance = Math.hypot(clientX - (rect.left + slot.x), clientY - (rect.top + slot.y));
    if (distance < nearestDistance) {
      nearestIndex = index;
      nearestDistance = distance;
    }
  });
  return nearestIndex;
}

interface PreviewDrag {
  buttonId: string;
  clientX: number;
  clientY: number;
  targetIndex: number | null;
}

export function MobileAssistantSettings() {
  const [, refresh] = useReducer((version: number) => version + 1, 0);
  const [draft, setDraft] = useState<ButtonDraft | null>(null);
  const [recording, setRecording] = useState(false);
  const [previewDrag, setPreviewDrag] = useState<PreviewDrag | null>(null);
  const circleRef = useRef<HTMLDivElement>(null);
  const previewDragRef = useRef<{
    pointerId: number;
    buttonId: string;
    buttons: MobileAssistantButton[];
    targetIndex: number | null;
  } | null>(null);
  const preferences = readMobileAssistantPreferences();
  const draggedButton = previewDrag
    ? preferences.buttons.find((button) => button.id === previewDrag.buttonId)
    : undefined;

  useEffect(() => {
    const onChanged = () => refresh();
    window.addEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, onChanged);
    window.addEventListener("storage", onChanged);
    return () => {
      window.removeEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, onChanged);
      window.removeEventListener("storage", onChanged);
    };
  }, []);

  useEffect(() => {
    if (!recording) return;
    setShortcutRecordingActive(true);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing) return;
      const chord = shortcutChordFromEvent(event);
      if (!chord) return;
      event.preventDefault();
      event.stopPropagation();
      setDraft((current) =>
        current ? { ...current, chord: normalizeRecordedPrimary(chord) } : current,
      );
      setRecording(false);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      setShortcutRecordingActive(false);
    };
  }, [recording]);

  const persistButtonSlot = (id: string, targetIndex: number) => {
    const buttons = moveButtonToSlot(preferences.buttons, id, targetIndex);
    if (buttons !== preferences.buttons) {
      writeMobileAssistantPreferences({ ...preferences, buttons });
    }
  };

  const beginPreviewDrag = (buttonId: string, event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || previewDragRef.current) return;
    event.preventDefault();
    const circle = circleRef.current;
    const buttons = [...preferences.buttons];
    const targetIndex = circle
      ? circleSlotFromPoint(event.clientX, event.clientY, circle, buttons.length)
      : null;
    previewDragRef.current = { pointerId: event.pointerId, buttonId, buttons, targetIndex };
    setPreviewDrag({ buttonId, clientX: event.clientX, clientY: event.clientY, targetIndex });
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const updatePreviewDrag = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = previewDragRef.current;
    const circle = circleRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !circle) return;
    event.preventDefault();
    const currentCount = readMobileAssistantPreferences().buttons.length;
    const targetIndex = circleSlotFromPoint(event.clientX, event.clientY, circle, currentCount);
    drag.targetIndex = targetIndex;
    setPreviewDrag({
      buttonId: drag.buttonId,
      clientX: event.clientX,
      clientY: event.clientY,
      targetIndex,
    });
  };

  const finishPreviewDrag = (event: PointerEvent<HTMLButtonElement>, commit: boolean) => {
    const drag = previewDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const circle = circleRef.current;
    const current = readMobileAssistantPreferences();
    const targetIndex =
      commit && circle
        ? circleSlotFromPoint(event.clientX, event.clientY, circle, current.buttons.length)
        : null;
    previewDragRef.current = null;
    setPreviewDrag(null);
    if (targetIndex !== null) {
      const buttons = moveButtonToSlot(current.buttons, drag.buttonId, targetIndex);
      if (buttons !== current.buttons) {
        writeMobileAssistantPreferences({ ...current, buttons });
      }
    }
  };

  const removeButton = (id: string) => {
    writeMobileAssistantPreferences({
      ...preferences,
      buttons: preferences.buttons.filter((button) => button.id !== id),
    });
  };

  const saveDraft = () => {
    if (!draft) return;
    const label = draft.label.trim();
    const binding = bindingFromDraft(draft);
    if (!label || (binding.kind === "text" && !binding.text.trim())) return;
    const next: MobileAssistantButton = {
      id: draft.id,
      label,
      binding,
      display: draft.display,
      icon: draft.icon,
      ...(draft.repeat && mobileAssistantBindingSupportsRepeat(binding) ? { repeat: true } : {}),
    };
    const index = preferences.buttons.findIndex((button) => button.id === draft.id);
    const buttons = [...preferences.buttons];
    if (index >= 0) buttons[index] = next;
    else if (buttons.length < MOBILE_ASSISTANT_MAX_BUTTONS) buttons.push(next);
    writeMobileAssistantPreferences({ ...preferences, buttons });
    setDraft(null);
  };

  return (
    <section>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-medium text-foreground">Mobile floating assistant</h3>
          <p
            className="mt-1 text-sm text-muted-foreground"
            title="Move a button freely, then release anywhere inside the circle to insert it. Releasing outside cancels."
          >
            Drag to reorder. Edge-drag to collapse.
          </p>
        </div>
        <Switch
          checked={preferences.enabled}
          onCheckedChange={(enabled) =>
            writeMobileAssistantPreferences({ ...preferences, enabled })
          }
          aria-label="Enable mobile floating assistant"
        />
      </div>

      <div className="mt-4 grid gap-4 rounded-xl border border-border p-3 sm:grid-cols-[240px_1fr]">
        <div
          ref={circleRef}
          data-testid="mobile-assistant-circle-preview"
          data-drop-valid={previewDrag ? previewDrag.targetIndex !== null : undefined}
          className="relative mx-auto h-[240px] w-[240px] touch-none rounded-full bg-muted/35 ring-offset-background transition-shadow data-[drop-valid=false]:ring-2 data-[drop-valid=false]:ring-destructive/45 data-[drop-valid=true]:ring-2 data-[drop-valid=true]:ring-primary/35"
          aria-label="Floating assistant circle preview"
        >
          {layoutMobileAssistantActions(
            preferences.buttons.length,
            { x: 120, y: 120 },
            { width: 240, height: 240 },
          ).map((position, index) => {
            const button = preferences.buttons[index];
            if (!button) return null;
            return (
              <button
                type="button"
                key={button.id}
                aria-label={`${button.label}, position ${index + 1}; drag to reorder`}
                data-testid={`mobile-assistant-preview-button-${button.id}`}
                data-dragging={previewDrag?.buttonId === button.id || undefined}
                data-drop-target={previewDrag?.targetIndex === index || undefined}
                onPointerDown={(event) => beginPreviewDrag(button.id, event)}
                onPointerMove={updatePreviewDrag}
                onPointerUp={(event) => finishPreviewDrag(event, true)}
                onPointerCancel={(event) => finishPreviewDrag(event, false)}
                onLostPointerCapture={(event) => finishPreviewDrag(event, false)}
                onKeyDown={(event) => {
                  if (!["ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"].includes(event.key))
                    return;
                  event.preventDefault();
                  const offset = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
                  const target =
                    (index + offset + preferences.buttons.length) % preferences.buttons.length;
                  persistButtonSlot(button.id, target);
                }}
                className="absolute flex h-9 w-9 touch-none cursor-grab items-center justify-center rounded-full border border-border bg-popover px-1 text-[10px] font-semibold text-popover-foreground shadow-sm transition-[left,top,transform,opacity] focus-visible:ring-2 focus-visible:ring-ring active:cursor-grabbing data-[dragging=true]:opacity-25 data-[drop-target=true]:border-primary data-[drop-target=true]:ring-2 data-[drop-target=true]:ring-primary/45"
                style={{
                  left: position.x,
                  top: position.y,
                  transform: "translate(-50%, -50%)",
                }}
                title={`${index + 1}. ${button.label}`}
              >
                <MobileAssistantButtonContent button={button} className="h-3.5 w-3.5" />
                <span className="absolute -right-1 -top-1 flex size-4 items-center justify-center rounded-full bg-primary text-[9px] text-primary-foreground">
                  {index + 1}
                </span>
              </button>
            );
          })}
          <div className="absolute left-1/2 top-1/2 flex size-[52px] -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full bg-primary text-primary-foreground shadow">
            <KeyboardIcon className="size-5" aria-hidden />
          </div>
        </div>
        <div className="grid content-center gap-3">
          <p className="text-sm font-medium text-foreground">Drag to arrange</p>
          <p
            className="text-xs text-muted-foreground"
            title="The button follows your pointer. Release outside the circle to keep the current order."
          >
            Release inside to place · outside to cancel
          </p>
        </div>
      </div>

      {previewDrag && draggedButton
        ? createPortal(
            <div
              aria-hidden
              data-testid="mobile-assistant-drag-preview"
              className="pointer-events-none fixed z-[100] flex h-9 w-9 items-center justify-center rounded-full border border-primary bg-popover px-1 text-[10px] font-semibold text-popover-foreground shadow-lg ring-2 ring-primary/35"
              style={{
                left: previewDrag.clientX,
                top: previewDrag.clientY,
                transform: "translate(-50%, -50%) scale(1.08)",
              }}
            >
              <MobileAssistantButtonContent button={draggedButton} className="h-3.5 w-3.5" />
            </div>,
            document.body,
          )
        : null}

      <div className="mt-3 overflow-hidden rounded-xl border border-border">
        {preferences.buttons.length === 0 ? (
          <p className="px-3 py-4 text-sm text-muted-foreground">No assistant buttons yet.</p>
        ) : (
          <ul>
            {preferences.buttons.map((button, index) => (
              <li
                key={button.id}
                className="flex min-h-12 items-center gap-1.5 border-b border-border/60 px-2 py-1.5 last:border-b-0"
              >
                <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground">
                  {index + 1}
                </span>
                <span className="w-16 shrink-0 truncate text-sm font-medium text-foreground">
                  {button.label}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                  {bindingSummary(button)}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Edit ${button.label}`}
                  onClick={() => setDraft(draftFromButton(button))}
                >
                  <PencilIcon />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Delete ${button.label}`}
                  onClick={() => removeButton(button.id)}
                >
                  <Trash2Icon />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={preferences.buttons.length >= MOBILE_ASSISTANT_MAX_BUTTONS}
          onClick={() => setDraft(draftFromButton())}
        >
          <PlusIcon /> Add button
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() =>
            writeMobileAssistantPreferences({
              ...preferences,
              buttons: defaultMobileAssistantButtons(),
            })
          }
        >
          <RotateCcwIcon /> Restore default buttons
        </Button>
        <span className="self-center text-xs text-muted-foreground">
          {preferences.buttons.length}/{MOBILE_ASSISTANT_MAX_BUTTONS}
        </span>
      </div>

      <Dialog
        open={draft !== null}
        onOpenChange={(open) => {
          if (!open) {
            setRecording(false);
            setDraft(null);
          }
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Assistant button</DialogTitle>
            <DialogDescription>Choose an action, key, or phrase.</DialogDescription>
          </DialogHeader>
          {draft ? (
            <div className="grid gap-4">
              <label className="grid gap-1.5 text-sm">
                Button label
                <Input
                  value={draft.label}
                  maxLength={24}
                  onChange={(event) =>
                    setDraft((current) =>
                      current ? { ...current, label: event.target.value } : current,
                    )
                  }
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                Button display
                <select
                  aria-label="Button display"
                  value={draft.display}
                  onChange={(event) =>
                    setDraft((current) =>
                      current
                        ? { ...current, display: event.target.value as "text" | "icon" }
                        : current,
                    )
                  }
                  className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm"
                >
                  <option value="text">Text label</option>
                  <option value="icon">Icon</option>
                </select>
              </label>
              {draft.display === "icon" ? (
                <label className="grid gap-1.5 text-sm">
                  Button icon
                  <select
                    aria-label="Button icon"
                    value={draft.icon}
                    onChange={(event) =>
                      setDraft((current) =>
                        current
                          ? { ...current, icon: event.target.value as MobileAssistantIcon }
                          : current,
                      )
                    }
                    className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm"
                  >
                    {MOBILE_ASSISTANT_ICONS.map((icon) => (
                      <option key={icon.id} value={icon.id}>
                        {icon.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <label className="grid gap-1.5 text-sm">
                Binding type
                <select
                  value={draft.kind}
                  onChange={(event) =>
                    setDraft((current) =>
                      current ? { ...current, kind: event.target.value as BindingKind } : current,
                    )
                  }
                  className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm"
                >
                  <option value="shortcut">OmniGent action</option>
                  <option value="key">Custom key combination</option>
                  <option value="text">Text or phrase</option>
                </select>
              </label>

              {draft.kind === "shortcut" ? (
                <label className="grid gap-1.5 text-sm">
                  Action from keyboard shortcuts
                  <select
                    value={draft.actionId}
                    onChange={(event) =>
                      setDraft((current) =>
                        current
                          ? { ...current, actionId: event.target.value as ShortcutActionId }
                          : current,
                      )
                    }
                    className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm"
                  >
                    {SHORTCUT_ACTION_IDS.map((actionId) => (
                      <option key={actionId} value={actionId}>
                        {DEFAULT_SHORTCUT_DEFINITIONS[actionId].label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}

              {draft.kind === "key" ? (
                <div className="grid gap-1.5 text-sm">
                  <span>Key combination</span>
                  <button
                    type="button"
                    aria-label="Record custom key combination"
                    aria-pressed={recording}
                    onClick={() => setRecording((current) => !current)}
                    className="flex min-h-10 items-center justify-center gap-1 rounded-lg border border-input px-3 hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {recording ? (
                      <span className="animate-pulse text-primary">Press shortcut…</span>
                    ) : (
                      shortcutBindingLabels(draft.chord).map((label) => (
                        <Kbd key={label}>{label}</Kbd>
                      ))
                    )}
                  </button>
                  <p
                    className="text-xs text-muted-foreground"
                    title="Browsers do not allow a web page to invoke protected browser or operating-system shortcuts such as opening a new tab."
                  >
                    In-app keys only.
                  </p>
                </div>
              ) : null}

              {draft.kind === "text" ? (
                <>
                  <label className="grid gap-1.5 text-sm">
                    Text or phrase
                    <Textarea
                      value={draft.text}
                      maxLength={2000}
                      rows={3}
                      placeholder="/compact"
                      onChange={(event) =>
                        setDraft((current) =>
                          current ? { ...current, text: event.target.value } : current,
                        )
                      }
                    />
                  </label>
                  <label className="flex items-center justify-between gap-3 text-sm">
                    Send after insert
                    <Switch
                      checked={draft.submit}
                      onCheckedChange={(submit) =>
                        setDraft((current) => (current ? { ...current, submit } : current))
                      }
                      aria-label="Send phrase immediately after inserting"
                    />
                  </label>
                </>
              ) : null}

              {mobileAssistantBindingSupportsRepeat(bindingFromDraft(draft)) ? (
                <label className="flex items-center justify-between gap-3 text-sm">
                  Repeat while held
                  <Switch
                    checked={draft.repeat}
                    onCheckedChange={(repeat) =>
                      setDraft((current) => (current ? { ...current, repeat } : current))
                    }
                    aria-label="Repeat while held"
                  />
                </label>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDraft(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={!draft?.label.trim() || (draft.kind === "text" && !draft.text.trim())}
              onClick={saveDraft}
            >
              Save button
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
