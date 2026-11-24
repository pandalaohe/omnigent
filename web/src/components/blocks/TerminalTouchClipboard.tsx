// Touch copy and paste for canvas-rendered terminals, which offer touch
// browsers no native selection or paste target.

import { useCallback, useEffect, useId, useRef, useState, type ClipboardEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  isTerminalClipboardWritePending,
  writeExplicitTerminalClipboardText,
} from "@/lib/terminalClipboardWriter";

export const TERMINAL_COPY_BLOCKED_MESSAGE = "Copying from this terminal is blocked.";

export function TerminalCopySheet({
  text,
  isCopyBlocked,
  onClose,
}: {
  text: string | null;
  isCopyBlocked: () => boolean;
  onClose: () => void;
}) {
  const textRef = useRef<HTMLPreElement | null>(null);
  const [selection, setSelection] = useState("");
  const open = text !== null;
  // Counts openings so a copy that finishes late can't close a reopened sheet.
  const openingRef = useRef(0);
  const isCopyAllowed = () => !isCopyBlocked();

  // Track the selection inside the sheet. Tapping the Copy button can collapse
  // it on touch devices, so keep the last one until a tap inside the text clears it.
  useEffect(() => {
    if (!open) {
      setSelection("");
      return;
    }
    openingRef.current += 1;
    const onSelectionChange = () => {
      const current = document.getSelection();
      const node = textRef.current;
      if (!current || !node || current.rangeCount === 0) return;
      if (node.contains(current.anchorNode) && node.contains(current.focusNode)) {
        setSelection(current.toString());
      } else if (!current.isCollapsed) {
        // A selection dragged out of the text no longer matches the last one.
        setSelection("");
      }
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, [open]);

  // Start at the newest output, the part a user usually wants.
  const scrollToEnd = useCallback((node: HTMLPreElement | null) => {
    textRef.current = node;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  const copy = async () => {
    if (text === null) return;
    if (isCopyBlocked()) {
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    const opening = openingRef.current;
    if (!(await writeExplicitTerminalClipboardText(selection || text, isCopyAllowed))) {
      toast.error("Couldn't copy. Select the text and choose Copy from the menu.");
      return;
    }
    toast.success(selection ? "Copied selection" : "Copied terminal text");
    if (openingRef.current === opening) onClose();
  };

  const onNativeCopy = (event: ClipboardEvent<HTMLPreElement>) => {
    if (isCopyBlocked()) {
      event.preventDefault();
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    // The browser copies the selection now. A program copy still in flight could
    // land after it, so write the selection again once that copy finishes.
    const selected = document.getSelection()?.toString();
    if (selected && isTerminalClipboardWritePending()) {
      void writeExplicitTerminalClipboardText(selected, isCopyAllowed, true);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="terminal-copy-sheet" className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Copy terminal text</DialogTitle>
          <DialogDescription>Press and hold to select text, or copy everything.</DialogDescription>
        </DialogHeader>
        {text ? (
          <pre
            ref={scrollToEnd}
            data-testid="terminal-copy-text"
            onCopy={onNativeCopy}
            className="max-h-[50vh] min-h-0 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap select-text"
          >
            {text}
          </pre>
        ) : (
          <p className="text-muted-foreground">This terminal has no text yet.</p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button
            type="button"
            disabled={!text}
            onClick={() => void copy()}
            componentId="terminal.touch_clipboard.copy_text"
          >
            {selection ? "Copy selection" : "Copy all"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function TerminalPasteBox({
  text,
  enabled,
  onChange,
  onSend,
  onClose,
}: {
  text: string | null;
  enabled: boolean;
  onChange: (text: string) => void;
  onSend: () => void;
  onClose: () => void;
}) {
  const descriptionId = useId();

  return (
    <Dialog open={text !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="terminal-paste-box">
        <DialogHeader>
          <DialogTitle>Paste into terminal</DialogTitle>
          <DialogDescription id={descriptionId}>
            Type, paste or edit your text, then send it to the terminal.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          autoFocus
          value={text ?? ""}
          onChange={(event) => onChange(event.target.value)}
          aria-label="Text to paste"
          aria-describedby={descriptionId}
          className="min-h-24 font-mono"
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!enabled || !text}
            onClick={onSend}
            componentId="terminal.touch_clipboard.paste_box"
          >
            Send to terminal
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
