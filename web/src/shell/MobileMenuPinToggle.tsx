import { PinIcon, PinOffIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface MobileMenuPinToggleProps {
  /** Stable pin id (`mobileMenuPins`), used for the test id. */
  itemId: string;
  /** The row's label, so the control reads "Pin Shells" / "Unpin Shells". */
  label: string;
  pinned: boolean;
  onToggle: () => void;
  className?: string;
}

/**
 * The trailing pin control inside a phone menu row: tapping it pins or unpins
 * that row without running the row's action.
 *
 * The parent is a Radix `DropdownMenuItem`, which selects on the click that
 * bubbles out of this button. Preventing the default and stopping propagation
 * keeps the tap on the toggle alone; pointer-down is deliberately left to
 * bubble so the item records the press and doesn't synthesise a second click.
 *
 * The accessible name states the action ("Pin Shells" / "Unpin Shells") after
 * the neighbouring Mute/Unmute rows, so no aria-pressed is layered on top.
 */
export function MobileMenuPinToggle({
  itemId,
  label,
  pinned,
  onToggle,
  className,
}: MobileMenuPinToggleProps) {
  return (
    <button
      type="button"
      aria-label={pinned ? `Unpin ${label}` : `Pin ${label}`}
      data-testid={`pin-menu-item-${itemId}`}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onToggle();
      }}
      className={cn(
        // 44 px hit area without growing the row: the negative margins take
        // back the row's own padding.
        "-my-2 -mr-1 flex size-11 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:text-foreground",
        className,
      )}
    >
      {pinned ? (
        <PinOffIcon className="size-4" aria-hidden />
      ) : (
        <PinIcon className="size-4" aria-hidden />
      )}
    </button>
  );
}
