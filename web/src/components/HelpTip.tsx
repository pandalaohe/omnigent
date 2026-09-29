import { useEffect, useRef, useState, type ReactNode } from "react";
import { CircleHelpIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** Grace window for the mouse to cross from the trigger into the popover. */
const HOVER_CLOSE_DELAY_MS = 150;

/**
 * A "?" hint with a popover body. Popover rather than Tooltip because touch
 * has no hover: tap toggles it, a mouse pointer also opens it on hover, and
 * Escape or an outside click closes it (Radix defaults). A short close delay
 * on pointer leave keeps it open while the pointer travels from the trigger
 * into the content, and re-entering either side cancels the pending close.
 */
export function HelpTip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const closeTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    };
  }, []);

  function cancelClose() {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }

  function scheduleClose() {
    cancelClose();
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      setOpen(false);
    }, HOVER_CLOSE_DELAY_MS);
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className="inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground"
          onPointerEnter={(event) => {
            if (event.pointerType !== "mouse") return;
            cancelClose();
            setOpen(true);
          }}
          onPointerLeave={(event) => {
            if (event.pointerType !== "mouse") return;
            scheduleClose();
          }}
        >
          <CircleHelpIcon className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        className="w-64 text-xs"
        onPointerEnter={(event) => {
          if (event.pointerType !== "mouse") return;
          cancelClose();
        }}
        onPointerLeave={(event) => {
          if (event.pointerType !== "mouse") return;
          scheduleClose();
        }}
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}
