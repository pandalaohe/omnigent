// Chromium delivers a macOS dead key (` / accented chars) to the input method
// before keydown, so preventDefault cannot stop the accent a shortcut fired under.

interface DiscardedElement {
  element: HTMLInputElement | HTMLTextAreaElement;
  value: string;
  selectionStart: number | null;
  selectionEnd: number | null;
}

const ARM_TIMEOUT_MS = 1000;

const DISCARDED_EVENT_TYPES = new Set([
  "compositionupdate",
  "compositionend",
  "beforeinput",
  "textInput",
  "input",
  "change",
]);

const RESTORING_EVENT_TYPES = new Set(["focusout", "focusin", "blur", "focus"]);

export function installDeadKeyShortcutGuard(
  win: Window,
  isShortcut: (event: KeyboardEvent) => boolean,
): () => void {
  let armed: KeyboardEvent | null = null;
  let armTimer: number | null = null;
  let finishTimer: number | null = null;
  let discarded: DiscardedElement | null = null;
  let restoring = false;

  const clearTimer = (timer: number | null): null => {
    if (timer !== null) win.clearTimeout(timer);
    return null;
  };

  const disarm = () => {
    armed = null;
    armTimer = clearTimer(armTimer);
  };

  const finish = () => {
    const current = discarded;
    if (!current) return;
    finishTimer = clearTimer(finishTimer);
    restoring = true;
    try {
      const { element } = current;
      // Focus is decided here, not at compositionstart: the user may have moved
      // on while the discard was pending.
      const stillFocused = win.document.activeElement === element;
      if (stillFocused) {
        // Blur first: it commits the pending composition and makes the input
        // method drop the accent it was about to insert.
        element.blur();
      }
      element.value = current.value;
      if (current.selectionStart !== null && current.selectionEnd !== null) {
        element.setSelectionRange(current.selectionStart, current.selectionEnd);
      }
      if (stillFocused && element.isConnected) {
        element.focus({ preventScroll: true });
      }
    } finally {
      restoring = false;
      discarded = null;
    }
  };

  const onKeyDown = (event: KeyboardEvent) => {
    // A keydown means the user resumed typing: settle the discard first so the
    // key's beforeinput/input are not swallowed as part of the accent.
    if (discarded !== null) finish();
    if (event.key === "Dead" && isShortcut(event)) {
      disarm();
      armed = event;
      armTimer = win.setTimeout(() => {
        if (armed === event) disarm();
      }, ARM_TIMEOUT_MS);
      return;
    }
    if (armed !== null) disarm();
  };

  const onKeyUp = (event: KeyboardEvent) => {
    if (armed !== null && event.code === armed.code) disarm();
  };

  const onCompositionStart = (event: CompositionEvent) => {
    const shortcutEvent = armed;
    disarm();
    if (
      shortcutEvent === null ||
      !shortcutEvent.defaultPrevented ||
      !(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)
    ) {
      return;
    }
    const element = event.target;
    discarded = {
      element,
      value: element.value,
      selectionStart: element.selectionStart,
      selectionEnd: element.selectionEnd,
    };
    event.stopImmediatePropagation();
    finishTimer = clearTimer(finishTimer);
    finishTimer = win.setTimeout(finish, 50);
  };

  const onGuardedEvent = (event: Event) => {
    const current = discarded;
    if (current === null || event.target !== current.element) return;
    if (restoring) {
      event.stopImmediatePropagation();
      return;
    }
    if (!DISCARDED_EVENT_TYPES.has(event.type)) return;
    event.stopImmediatePropagation();
    if (event.cancelable) event.preventDefault();
    if (event.type === "input") finish();
  };

  win.addEventListener("keydown", onKeyDown, true);
  win.addEventListener("keyup", onKeyUp, true);
  win.addEventListener("compositionstart", onCompositionStart, true);
  const guardedTypes = [...DISCARDED_EVENT_TYPES, ...RESTORING_EVENT_TYPES];
  for (const type of guardedTypes) win.addEventListener(type, onGuardedEvent, true);

  return () => {
    // Settle a pending discard while the listeners can still swallow its events.
    if (discarded !== null) finish();
    win.removeEventListener("keydown", onKeyDown, true);
    win.removeEventListener("keyup", onKeyUp, true);
    win.removeEventListener("compositionstart", onCompositionStart, true);
    for (const type of guardedTypes) win.removeEventListener(type, onGuardedEvent, true);
    disarm();
    finishTimer = clearTimer(finishTimer);
    discarded = null;
  };
}
