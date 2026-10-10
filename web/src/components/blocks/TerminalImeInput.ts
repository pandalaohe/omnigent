import type { Terminal } from "@xterm/xterm";
import { terminalTextareaEdit, type TerminalTextareaSnapshot } from "./terminalTextareaEdit";

interface PendingEdit {
  kind: "key" | "input";
  before: TerminalTextareaSnapshot;
}

const EDIT_INPUT_TYPES = new Set([
  "insertText",
  "insertReplacementText",
  "deleteContentBackward",
  "deleteContentForward",
  "deleteWordBackward",
  "deleteWordForward",
]);

/** Reconcile mobile DOM edits without treating the IME's caret as the line end. */
export class TerminalImeInput {
  private readonly term: Terminal;
  private readonly listeners = new AbortController();
  private compositionTimer: ReturnType<typeof setTimeout> | null = null;
  private pending: PendingEdit | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private nativeKey = false;
  private composing = false;

  constructor(term: Terminal) {
    this.term = term;
    const { signal } = this.listeners;
    const textarea = term.textarea;
    const root = term.element;
    // Root capture runs before xterm's textarea-capture input handler.
    root?.addEventListener("beforeinput", this.beforeInput, { capture: true, signal });
    root?.addEventListener("input", this.input, { capture: true, signal });
    root?.addEventListener("paste", this.paste, { capture: true, signal });
    root?.addEventListener("blur", this.blur, { capture: true, signal });
    textarea?.addEventListener("compositionstart", this.compositionStart, { signal });
    textarea?.addEventListener("compositionend", this.compositionEnd, { signal });
  }

  get isComposing(): boolean {
    return this.composing;
  }

  private get enabled(): boolean {
    return navigator.maxTouchPoints > 0 && !this.term.options.screenReaderMode;
  }

  private snapshot(): TerminalTextareaSnapshot {
    const textarea = this.term.textarea!;
    return {
      value: textarea.value,
      selectionStart: textarea.selectionStart,
      selectionEnd: textarea.selectionEnd,
    };
  }

  /** Return false only when the browser IME owns this keyboard event. */
  handleKeyEvent(event: KeyboardEvent): boolean {
    if (!this.enabled) return true;
    if (this.composing || event.isComposing) return true;
    const processing = event.keyCode === 229 || event.key === "Process";
    if (processing) {
      this.nativeKey = false;
      if (this.compositionTimer !== null) return true;
      if (event.type === "keydown" && !this.pending) this.start("key");
      if (event.type === "keyup" && this.pending?.kind === "key") this.flush(false);
      return false;
    }
    if (event.type === "keyup") {
      this.nativeKey = false;
      if (this.pending?.kind === "key") {
        this.flush(false);
        return false;
      }
    } else if (event.type === "keydown") {
      this.flush();
      this.nativeKey = true;
    }
    return true;
  }

  private start(kind: PendingEdit["kind"]): void {
    this.pending = { kind, before: this.snapshot() };
    this.schedule(80);
  }

  private schedule(delay: number): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, delay);
  }

  private flush(clearEmpty = true): void {
    if (this.composing) return;
    if (!this.pending || !this.enabled) {
      this.clear();
      return;
    }
    const data = terminalTextareaEdit(
      this.pending.before,
      this.snapshot(),
      this.term.modes.applicationCursorKeysMode,
    );
    if (!data && !clearEmpty) return;
    this.clear();
    if (data) this.term.input(data, true);
  }

  private clear(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.pending = null;
  }

  private compositionStart = (): void => {
    if (this.enabled) this.flush();
    this.clearCompositionTimer();
    this.nativeKey = false;
    this.composing = true;
  };

  private compositionEnd = (): void => {
    this.composing = false;
    // xterm still owns a commit input dispatched after compositionend.
    this.clearCompositionTimer();
    this.compositionTimer = setTimeout(() => {
      this.compositionTimer = null;
    }, 0);
  };

  private beforeInput = (event: InputEvent): void => {
    if (
      event.target !== this.term.textarea ||
      !this.enabled ||
      this.composing ||
      event.isComposing ||
      this.compositionTimer !== null
    )
      return;
    if (!EDIT_INPUT_TYPES.has(event.inputType)) return;
    if (this.nativeKey && event.inputType === "insertText") return;
    if (!this.pending) this.start("input");
  };

  private input = (event: InputEvent): void => {
    if (
      event.target !== this.term.textarea ||
      !this.enabled ||
      this.composing ||
      event.isComposing ||
      this.compositionTimer !== null
    )
      return;
    if (this.pending) {
      event.stopPropagation();
      this.schedule(this.pending.kind === "key" ? 80 : 0);
      return;
    }
    if (!this.nativeKey && event.inputType === "insertText" && event.data) {
      event.stopPropagation();
      this.term.input(event.data, true);
    }
  };

  private paste = (): void => {
    if (this.enabled) this.flush();
  };

  private blur = (event: FocusEvent): void => {
    if (event.target !== this.term.textarea || !this.enabled) return;
    this.nativeKey = false;
    // xterm clears the textarea in its blur listener; settle before that happens.
    this.flush();
    if (this.composing) this.compositionEnd();
  };

  private clearCompositionTimer(): void {
    if (this.compositionTimer !== null) clearTimeout(this.compositionTimer);
    this.compositionTimer = null;
  }

  dispose(): void {
    this.listeners.abort();
    this.clearCompositionTimer();
    this.clear();
    this.composing = false;
  }
}
