import { useEffect, useRef, useState, type ReactNode } from "react";
import { CircleHelpIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

/** Grace window for the mouse to cross from the trigger into the popover. */
const HOVER_CLOSE_DELAY_MS = 150;

/**
 * A "?" hint with a popover body. Popover rather than Tooltip because touch
 * has no hover: tap toggles it, a mouse pointer also opens it on hover, and
 * Escape or an outside click closes it (Radix defaults). A short close delay
 * on pointer leave keeps it open while the pointer travels from the trigger
 * into the content, and re-entering either side cancels the pending close.
 */
export function HelpTip({
  label,
  children,
  className,
  icon,
}: {
  label: string;
  children: ReactNode;
  className?: string;
  icon?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const closeTimerRef = useRef<number | null>(null);
  const pointerFocusRef = useRef(false);

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
          className={cn(
            "inline-flex size-4 shrink-0 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground",
            className,
          )}
          onPointerDown={() => {
            pointerFocusRef.current = true;
          }}
          onFocus={() => {
            cancelClose();
            if (pointerFocusRef.current) {
              pointerFocusRef.current = false;
              return;
            }
            setOpen(true);
          }}
          onBlur={() => {
            pointerFocusRef.current = false;
            scheduleClose();
          }}
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
          {icon ?? <CircleHelpIcon className="size-3.5" />}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        className="w-64 text-xs"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
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
