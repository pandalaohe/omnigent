import type { Terminal } from "@xterm/xterm";

/** Forward mobile input-only symbols after keyboard and composition events. */
export class TerminalImeInput {
  private readonly term: Terminal;
  private readonly listeners = new AbortController();
  private compositionTimer: ReturnType<typeof setTimeout> | null = null;
  private nativeKey = false;
  private composing = false;

  constructor(term: Terminal) {
    this.term = term;
    const { signal } = this.listeners;
    const textarea = term.textarea;
    const root = term.element;
    // Root capture runs before xterm's textarea-capture input handler.
    root?.addEventListener("input", this.input, { capture: true, signal });
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

  /** Keyboard-driven input remains owned by xterm. */
  handleKeyEvent(event: KeyboardEvent): boolean {
    if (!this.enabled) return true;
    if (this.composing || event.isComposing) return false;
    if (event.type === "keydown") this.nativeKey = true;
    else if (event.type === "keyup") this.nativeKey = false;
    return true;
  }

  private compositionStart = (): void => {
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

  private input = (event: InputEvent): void => {
    if (
      event.target !== this.term.textarea ||
      !this.enabled ||
      this.nativeKey ||
      this.composing ||
      event.isComposing ||
      this.compositionTimer !== null
    )
      return;
    if (event.inputType === "insertText" && event.data) {
      event.stopPropagation();
      // Preserve xterm's scroll, selection, input-disable and activity semantics.
      this.term.input(event.data, true);
    }
  };

  private clearCompositionTimer(): void {
    if (this.compositionTimer !== null) clearTimeout(this.compositionTimer);
    this.compositionTimer = null;
  }

  private blur = (event: FocusEvent): void => {
    if (event.target !== this.term.textarea) return;
    this.nativeKey = false;
    if (this.composing) this.compositionEnd();
  };

  dispose(): void {
    this.listeners.abort();
    this.clearCompositionTimer();
    this.composing = false;
  }
}
