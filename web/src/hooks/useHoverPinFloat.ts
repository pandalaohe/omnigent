import { useEffect, useRef, useState, type PointerEvent } from "react";

/** Grace window for the mouse to cross from the trigger into the popover. */
const HOVER_CLOSE_DELAY_MS = 150;

/**
 * Float behaviour for a hover-or-tap popover: a mouse pointer opens it on hover,
 * a click pins it, a click beside it or Escape clears it (Radix defaults). A
 * short close delay on pointer leave keeps it open while the pointer travels
 * from the trigger into the content, and re-entering either side cancels the
 * pending close. Touch has no hover, so a tap pins it.
 */
export function useHoverPinFloat() {
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

  const triggerHandlers = {
    onPointerDown: () => {
      pointerFocusRef.current = true;
    },
    onFocus: () => {
      cancelClose();
      if (pointerFocusRef.current) {
        pointerFocusRef.current = false;
        return;
      }
      setOpen(true);
    },
    onBlur: () => {
      pointerFocusRef.current = false;
      scheduleClose();
    },
    onPointerEnter: (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      cancelClose();
      setOpen(true);
    },
    onPointerLeave: (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      scheduleClose();
    },
  };

  const contentHandlers = {
    onPointerEnter: (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      cancelClose();
    },
    onPointerLeave: (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      scheduleClose();
    },
  };

  return { open, setOpen, triggerHandlers, contentHandlers };
}
