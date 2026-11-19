import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { toast } from "sonner";
import type { TerminalSession } from "@/components/blocks/TerminalSession";
import type { ClipboardOp } from "./terminalKeys";

const HOST_CLIPBOARD_MAX_BYTES = 256 * 1024;
const HOST_REPLY_TIMEOUT_MS = 5000;
const PASTE_REPEAT_MS = 3000;
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

export type TerminalClipboardAction = (op: ClipboardOp, options: TerminalPasteOptions) => void;

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
  bracketed: boolean;
  repeatUntil: number;
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
  const [showPasteBox, setShowPasteBox] = useState(false);
  const cancel = useCallback((notifyDisconnect = false) => {
    const request = requestRef.current;
    const readingHost = request?.source === "pasteHost" && !reviewRef.current;
    request?.controller.abort();
    requestRef.current = null;
    reviewRef.current = null;
    setReview(null);
    setShowPasteBox(false);
    if (notifyDisconnect && readingHost) toast.error(NOT_CONNECTED);
  }, []);

  useLayoutEffect(() => {
    cancel();
    return () => requestRef.current?.controller.abort();
  }, [scope, active, readOnly, cancel]);

  useEffect(() => {
    if (!review?.repeatUntil) return;
    const timer = setTimeout(
      () => {
        if (reviewRef.current !== review) return;
        const expired = { ...review, repeatUntil: 0 };
        reviewRef.current = expired;
        setReview(expired);
      },
      Math.max(0, review.repeatUntil - performance.now()),
    );
    return () => clearTimeout(timer);
  }, [review]);

  const isCurrent = (request: PasteRequest) =>
    requestRef.current === request &&
    !request.controller.signal.aborted &&
    request.session === sessionRef.current &&
    request.session.canPaste();

  const send = (request: PasteRequest, text: string, enter: boolean) => {
    if (!isCurrent(request)) return;
    cancel();
    if (!request.session.paste(text, enter)) {
      toast.error(NOT_CONNECTED);
      return;
    }
    request.session.focus();
  };

  const offerText = (request: PasteRequest, raw: string) => {
    if (!isCurrent(request)) return;
    setShowPasteBox(false);
    const text = stripPasteControlCharacters(raw).replace(/\n+$/, "");
    if (!text) {
      cancel();
      toast.info("Clipboard is empty");
    } else if (/[\n\t]/.test(text)) {
      const next = {
        request,
        text,
        bracketed: request.session.pastesBracketed(),
        repeatUntil: 0,
      };
      if (next.bracketed && request.keyId !== undefined)
        next.repeatUntil = performance.now() + PASTE_REPEAT_MS;
      reviewRef.current = next;
      setReview(next);
    } else {
      send(request, text, request.enter);
    }
  };

  const paste = async (source: PasteRequest["source"], options: TerminalPasteOptions) => {
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
      if (!pending.bracketed || !session.pastesBracketed()) return;
      if (performance.now() <= pending.repeatUntil) {
        send(pending.request, pending.text, false);
        return;
      }
    }

    cancel();
    const request = { ...options, source, session, controller: new AbortController() };
    requestRef.current = request;
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
    try {
      offerText(request, await navigator.clipboard.readText());
    } catch {
      if (isCurrent(request)) setShowPasteBox(true);
    }
  };

  return {
    paste,
    cancel,
    showPasteBox,
    pasteFromBox: (text: string) => {
      if (requestRef.current) offerText(requestRef.current, text);
    },
    review,
    confirm: () => {
      const current = reviewRef.current;
      if (current) send(current.request, current.text, false);
    },
  };
}
