import {
  isComposerTextarea,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";

/** Preserve the browser's undo entry when its textarea editing command is supported. */
export function replaceDictatedText(
  field: ComposerTextTarget | null,
  expected: string,
  start: number,
  end: number,
  text: string,
): void {
  if (!field || field.value !== expected) return;
  if (!isComposerTextarea(field)) {
    field.insertText(text, start, end);
    return;
  }
  if (!document.execCommand) return;
  const focused = document.activeElement;
  const selection = [field.selectionStart, field.selectionEnd] as const;
  field.focus({ preventScroll: true });
  field.setSelectionRange(start, end);
  try {
    document.execCommand("insertText", false, text);
  } catch {
    // Unsupported platforms use the caller's controlled-state replacement.
  } finally {
    if (focused instanceof HTMLElement && focused !== field) {
      focused.focus({ preventScroll: true });
      field.setSelectionRange(...selection);
    }
  }
}
