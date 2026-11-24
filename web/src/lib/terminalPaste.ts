import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { toast } from "sonner";
import type { TerminalSession } from "@/components/blocks/TerminalSession";
import {
  readTerminalKeyboardPreferences,
  terminalPasteBoxModeFor,
} from "./terminalKeyboardPreferences";
import type { ClipboardOp } from "./terminalKeys";

const HOST_CLIPBOARD_MAX_BYTES = 256 * 1024;
const HOST_REPLY_TIMEOUT_MS = 5000;
const NOT_CONNECTED = "Not connected";
const HOST_FAILURES: Readonly<Record<string, string>> = {
  "read-only": "Read-only terminal",
  empty: "Host clipboard is empty",
  unavailable: "Can't read the host clipboard",
  "too-large": "Too large to paste",
  busy: "Still reading",
};

export interface TerminalPasteOptions {
  enter: boolean;
  keyId?: string;
}

export type TerminalClipboardAction = (
  op: ClipboardOp,
  options: TerminalPasteOptions,
  nativeText?: string,
) => void;

// C0/C1 controls can end bracketed paste or send a key sequence.
// oxlint-disable-next-line no-control-regex
const PASTE_CONTROL_CHARACTERS = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g;

export function stripPasteControlCharacters(text: string): string {
  return text.replace(/\r\n?/g, "\n").replace(PASTE_CONTROL_CHARACTERS, "");
}

type HostClipboardResult = { text: string } | { error: string };

/** The request ID and callback live on the socket that sent them. */
export class HostClipboardConnection {
  private readonly socket: WebSocket;
  private supported = false;
  private pending?: { id: string; finish: (result: HostClipboardResult) => void };

  constructor(socket: WebSocket) {
    this.socket = socket;
  }

  receive(data: string): boolean {
    // Terminal output is binary. Avoid parsing unrelated protocol frames twice.
    if (!data.includes('"hostClipboard"') && !data.includes('"host-clipboard"')) return false;
    let frame;
    try {
      frame = JSON.parse(data);
    } catch {
      return false;
    }
    if (frame?.type === "capabilities" && frame.hostClipboard === 1) {
      this.supported = true;
      return true;
    }
    if (frame?.type !== "host-clipboard") return false;
    const pending = this.pending;
    if (!pending || frame.id !== pending.id) return true;
    if (frame.ok === true && typeof frame.text === "string") {
      pending.finish(
        frame.text.length <= HOST_CLIPBOARD_MAX_BYTES &&
          new TextEncoder().encode(frame.text).length <= HOST_CLIPBOARD_MAX_BYTES
          ? { text: frame.text }
          : { error: "Too large to paste" },
      );
    } else if (frame.ok === false && typeof frame.reason === "string") {
      pending.finish({
        error: Object.hasOwn(HOST_FAILURES, frame.reason)
          ? HOST_FAILURES[frame.reason]
          : HOST_FAILURES.unavailable,
      });
    }
    return true;
  }

  read(signal: AbortSignal): Promise<HostClipboardResult> {
    if (signal.aborted || this.socket.readyState !== WebSocket.OPEN)
      return Promise.resolve({ error: NOT_CONNECTED });
    if (!this.supported) return Promise.resolve({ error: "The host needs a newer version" });
    this.close();
    return new Promise((resolve) => {
      const id = crypto.getRandomValues(new Uint32Array(4)).join("-");
      const abort = () => finish({ error: NOT_CONNECTED });
      const finish = (result: HostClipboardResult) => {
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        this.pending = undefined;
        resolve(result);
      };
      const timer = setTimeout(
        () => finish({ error: "The host did not answer" }),
        HOST_REPLY_TIMEOUT_MS,
      );
      this.pending = { id, finish };
      signal.addEventListener("abort", abort, { once: true });
      try {
        this.socket.send(JSON.stringify({ type: "host-clipboard-read", id }));
      } catch {
        finish({ error: NOT_CONNECTED });
      }
    });
  }

  close(): void {
    this.pending?.finish({ error: NOT_CONNECTED });
  }
}

interface PasteRequest extends TerminalPasteOptions {
  session: TerminalSession;
  controller: AbortController;
  source: "paste" | "pasteHost";
}

interface PasteReview {
  request: PasteRequest;
  text: string;
}

/** Use the same normalization and confirmation for every clipboard source. */
export function useTerminalPaste(
  sessionRef: RefObject<TerminalSession | null>,
  scope: string,
  active: boolean,
  readOnly: boolean,
) {
  const requestRef = useRef<PasteRequest | null>(null);
  const reviewRef = useRef<PasteReview | null>(null);
  const [review, setReview] = useState<PasteReview | null>(null);
  const [pasteBoxText, setPasteBoxText] = useState<string | null>(null);
  const cancel = useCallback((notifyDisconnect = false) => {
    const request = requestRef.current;
    const readingHost = request?.source === "pasteHost" && !reviewRef.current;
    request?.controller.abort();
    requestRef.current = null;
    reviewRef.current = null;
    setReview(null);
    setPasteBoxText(null);
    if (notifyDisconnect && readingHost) toast.error(NOT_CONNECTED);
  }, []);

  useLayoutEffect(() => {
    cancel();
    return () => requestRef.current?.controller.abort();
  }, [scope, active, readOnly, cancel]);

  const isCurrent = (request: PasteRequest) =>
    requestRef.current === request &&
    !request.controller.signal.aborted &&
    request.session === sessionRef.current &&
    request.session.canPaste();

  const showReview = (request: PasteRequest, text: string) => {
    const next = { request, text };
    reviewRef.current = next;
    setReview(next);
  };

  const send = (request: PasteRequest, text: string, enter: boolean, reviewOnRefusal = false) => {
    if (!isCurrent(request)) return;
    const sent = request.session.paste(text, enter);
    if (!sent && reviewOnRefusal && isCurrent(request)) {
      showReview(request, text);
      return;
    }
    cancel();
    if (!sent) {
      toast.error(NOT_CONNECTED);
      return;
    }
    request.session.focus();
  };

  const offerText = (request: PasteRequest, raw: string) => {
    if (!isCurrent(request)) return;
    setPasteBoxText(null);
    const text = stripPasteControlCharacters(raw).replace(/\n+$/, "");
    const hasLineBreakOrTab = /[\n\t]/.test(text);
    if (!text) {
      cancel();
      toast.info("Clipboard is empty");
    } else if (hasLineBreakOrTab && !request.session.pastesBracketed()) {
      showReview(request, text);
    } else {
      send(request, text, request.enter && !hasLineBreakOrTab, hasLineBreakOrTab);
    }
  };

  const paste = async (
    source: PasteRequest["source"],
    options: TerminalPasteOptions,
    nativeText?: string,
  ) => {
    const session = sessionRef.current;
    if (!active) return;
    if (readOnly) {
      toast.error("Read-only terminal");
      return;
    }
    if (!session?.canPaste()) {
      toast.error(NOT_CONNECTED);
      return;
    }
    const pending = reviewRef.current;
    if (
      pending &&
      options.keyId !== undefined &&
      pending.request.keyId === options.keyId &&
      pending.request.source === source &&
      isCurrent(pending.request)
    ) {
      // Leave the current preview in place when its paste key is tapped again.
      return;
    }

    cancel();
    const request = { ...options, source, session, controller: new AbortController() };
    requestRef.current = request;
    if (nativeText !== undefined) {
      offerText(request, nativeText);
      return;
    }
    if (source === "pasteHost") {
      const result = await session.readHostClipboard(request.controller.signal);
      if (!isCurrent(request)) return;
      if ("error" in result) {
        cancel();
        toast.error(result.error);
      } else {
        offerText(request, result.text);
      }
      return;
    }
    const pasteBoxMode = terminalPasteBoxModeFor(readTerminalKeyboardPreferences());
    let text: string;
    try {
      text = await navigator.clipboard.readText();
    } catch {
      if (!isCurrent(request)) return;
      if (pasteBoxMode === "never") {
        cancel();
        toast.error("Couldn't read the clipboard. Use your browser's Paste command.");
      } else {
        setPasteBoxText("");
      }
      return;
    }
    if (!isCurrent(request)) return;
    if (pasteBoxMode === "always") {
      setPasteBoxText(text);
    } else {
      offerText(request, text);
    }
  };

  return {
    paste,
    cancel,
    pasteBoxText,
    setPasteBoxText,
    pasteFromBox: () => {
      if (requestRef.current && pasteBoxText !== null) offerText(requestRef.current, pasteBoxText);
    },
    review,
    confirm: () => {
      const current = reviewRef.current;
      if (current) send(current.request, current.text, false);
    },
  };
}
