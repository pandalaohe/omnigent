import { afterEach, describe, expect, it, vi } from "vitest";

import { installDeadKeyShortcutGuard } from "./deadKeyShortcutGuard";

function isAltBackquote(event: KeyboardEvent): boolean {
  return event.code === "Backquote" && event.altKey;
}

function dispatchDeadKey(target: EventTarget, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "Dead",
    code: "Backquote",
    altKey: true,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

function dispatchComposition(target: EventTarget, type: string): void {
  target.dispatchEvent(new CompositionEvent(type, { bubbles: true, cancelable: true }));
}

function dispatchInput(target: EventTarget): void {
  const event =
    typeof InputEvent === "function"
      ? new InputEvent("input", { bubbles: true, cancelable: true })
      : new Event("input", { bubbles: true, cancelable: true });
  target.dispatchEvent(event);
}

function dispatchBeforeInput(target: EventTarget): Event {
  const event =
    typeof InputEvent === "function"
      ? new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText" })
      : new Event("beforeinput", { bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

function dispatchKey(target: EventTarget, init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

function preventDefaultKeydown(event: KeyboardEvent): void {
  event.preventDefault();
}

interface GuardHarness {
  textarea: HTMLTextAreaElement;
  compositionStart: ReturnType<typeof vi.fn>;
  compositionUpdate: ReturnType<typeof vi.fn>;
  input: ReturnType<typeof vi.fn>;
  uninstallGuard: () => void;
  cleanup: () => void;
}

function mountGuard({ preventDefault = false } = {}): GuardHarness {
  const textarea = document.createElement("textarea");
  textarea.value = "hello";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.setSelectionRange(5, 5);

  const compositionStart = vi.fn();
  const compositionUpdate = vi.fn();
  const input = vi.fn();
  textarea.addEventListener("compositionstart", compositionStart);
  textarea.addEventListener("compositionupdate", compositionUpdate);
  textarea.addEventListener("input", input);

  if (preventDefault) window.addEventListener("keydown", preventDefaultKeydown);
  const uninstallGuard = installDeadKeyShortcutGuard(window, isAltBackquote);

  return {
    textarea,
    compositionStart,
    compositionUpdate,
    input,
    uninstallGuard,
    cleanup: () => {
      uninstallGuard();
      window.removeEventListener("keydown", preventDefaultKeydown);
      textarea.remove();
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("installDeadKeyShortcutGuard", () => {
  it("swallows the composition when an armed dead-key shortcut was prevented", () => {
    const harness = mountGuard({ preventDefault: true });
    const keydown = dispatchDeadKey(harness.textarea);
    expect(keydown.defaultPrevented).toBe(true);

    dispatchComposition(harness.textarea, "compositionstart");
    harness.textarea.value = "hello`";
    dispatchInput(harness.textarea);

    expect(harness.compositionStart).not.toHaveBeenCalled();
    expect(harness.compositionUpdate).not.toHaveBeenCalled();
    expect(harness.input).not.toHaveBeenCalled();
    expect(harness.textarea.value).toBe("hello");
    expect(harness.textarea.selectionStart).toBe(5);
    expect(harness.textarea.selectionEnd).toBe(5);
    expect(document.activeElement).toBe(harness.textarea);
    harness.cleanup();
  });

  it("lets the composition through when no handler acted on the chord", () => {
    const harness = mountGuard();
    dispatchDeadKey(harness.textarea);

    dispatchComposition(harness.textarea, "compositionstart");
    dispatchComposition(harness.textarea, "compositionupdate");
    harness.textarea.value = "hello`";
    dispatchInput(harness.textarea);

    expect(harness.compositionStart).toHaveBeenCalledOnce();
    expect(harness.compositionUpdate).toHaveBeenCalledOnce();
    expect(harness.input).toHaveBeenCalledOnce();
    expect(harness.textarea.value).toBe("hello`");
    harness.cleanup();
  });

  it("ignores dead keys that are not shortcuts", () => {
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea, { code: "KeyE", key: "Dead" });

    dispatchComposition(harness.textarea, "compositionstart");
    dispatchComposition(harness.textarea, "compositionupdate");
    harness.textarea.value = "hello`";
    dispatchInput(harness.textarea);

    expect(harness.compositionStart).toHaveBeenCalledOnce();
    expect(harness.input).toHaveBeenCalledOnce();
    expect(harness.textarea.value).toBe("hello`");
    harness.cleanup();
  });

  it("disarms when the shortcut code is released before composition", () => {
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea);
    harness.textarea.dispatchEvent(
      new KeyboardEvent("keyup", { code: "Backquote", bubbles: true }),
    );

    dispatchComposition(harness.textarea, "compositionstart");

    expect(harness.compositionStart).toHaveBeenCalledOnce();
    harness.cleanup();
  });

  it("disarms after the timeout", () => {
    vi.useFakeTimers();
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea);
    vi.advanceTimersByTime(1000);

    dispatchComposition(harness.textarea, "compositionstart");

    expect(harness.compositionStart).toHaveBeenCalledOnce();
    harness.cleanup();
  });

  it("stops guarding once uninstalled", () => {
    const harness = mountGuard({ preventDefault: true });
    harness.uninstallGuard();

    dispatchDeadKey(harness.textarea);
    dispatchComposition(harness.textarea, "compositionstart");

    expect(harness.compositionStart).toHaveBeenCalledOnce();
    harness.cleanup();
  });

  it("settles a pending discard on the next keydown so typed text is not swallowed", () => {
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea);
    dispatchComposition(harness.textarea, "compositionstart");
    harness.textarea.value = "hello`";

    const beforeInput = vi.fn();
    harness.textarea.addEventListener("beforeinput", beforeInput);

    dispatchKey(harness.textarea, { key: "a", code: "KeyA" });
    const typedBeforeInput = dispatchBeforeInput(harness.textarea);
    harness.textarea.value += "a";
    dispatchInput(harness.textarea);

    expect(typedBeforeInput.defaultPrevented).toBe(false);
    expect(beforeInput).toHaveBeenCalledOnce();
    expect(harness.input).toHaveBeenCalledOnce();
    expect(harness.textarea.value).toBe("helloa");
    expect(document.activeElement).toBe(harness.textarea);
    harness.cleanup();
  });

  it("does not steal focus back when the discard settles after focus moved", () => {
    vi.useFakeTimers();
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea);
    dispatchComposition(harness.textarea, "compositionstart");

    const otherInput = document.createElement("input");
    document.body.appendChild(otherInput);
    otherInput.focus();

    vi.advanceTimersByTime(1000);

    expect(document.activeElement).toBe(otherInput);
    expect(harness.textarea.value).toBe("hello");
    otherInput.remove();
    harness.cleanup();
  });

  it("restores the snapshot when uninstalled during a pending discard", () => {
    const harness = mountGuard({ preventDefault: true });
    dispatchDeadKey(harness.textarea);
    dispatchComposition(harness.textarea, "compositionstart");
    harness.textarea.value = "hello`";

    harness.uninstallGuard();

    expect(harness.textarea.value).toBe("hello");
    expect(document.activeElement).toBe(harness.textarea);
    harness.cleanup();
  });
});
