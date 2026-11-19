import { isShortcutRecordingActive } from "./keyboardShortcutPreferences";

type TerminalKeydown = (event: KeyboardEvent) => boolean;
const terminalKeydownByTarget = new WeakMap<HTMLTextAreaElement, TerminalKeydown>();

export function registerTerminalKeydown(
  textarea: HTMLTextAreaElement,
  handler: TerminalKeydown,
  signal: AbortSignal,
) {
  terminalKeydownByTarget.set(textarea, handler);
  signal.addEventListener("abort", () => terminalKeydownByTarget.delete(textarea), { once: true });
}

/** Install before app shortcut listeners so the focused terminal can claim its own chords. */
export function installTerminalHardwareInput(signal?: AbortSignal): void {
  window.addEventListener(
    "keydown",
    (event) => {
      if (isShortcutRecordingActive() || !(event.target instanceof HTMLTextAreaElement)) return;
      if (terminalKeydownByTarget.get(event.target)?.(event)) event.stopImmediatePropagation();
    },
    { capture: true, signal },
  );
}
