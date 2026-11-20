import type { ReactNode } from "react";
import { PinIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface MobileMenuPinToggleProps {
  /** Stable pin id (`mobileMenuPins`), used for the test id. */
  itemId: string;
  /** The row's label, so the control reads "Pin Shells" / "Unpin Shells". */
  label: string;
  pinned: boolean;
  onToggle: () => void;
  /** Take focus once it mounts (after the row moves between groups). */
  autoFocus?: boolean;
}

/**
 * The row height that holds this control inside the row's own bounds. A shorter
 * row lets the 44 px target reach into the row above or below it, where a tap
 * near the edge would flip the neighbour's pin instead of this one. Rows that
 * show a control carry it, at every UI font size.
 */
const PIN_ROW_CLASS = "min-h-11";

/**
 * The pin control that rides at the trailing edge of a phone menu row.
 *
 * Filled means pinned, outline means not, matching the design's state reading.
 * The accessible name states the action ("Pin Shells" / "Unpin Shells"), after
 * the Mute/Unmute rows beside it, so no aria-pressed is layered on top.
 */
export function MobileMenuPinToggle({
  itemId,
  label,
  pinned,
  onToggle,
  autoFocus = false,
}: MobileMenuPinToggleProps) {
  return (
    <button
      type="button"
      aria-label={pinned ? `Unpin ${label}` : `Pin ${label}`}
      data-testid={`pin-menu-item-${itemId}`}
      onClick={onToggle}
      autoFocus={autoFocus}
      className="flex size-11 max-h-full items-center justify-center rounded-md text-muted-foreground hover:text-foreground"
    >
      <PinIcon className={pinned ? "size-4 fill-current" : "size-4"} aria-hidden />
    </button>
  );
}

/** Reserve the trailing control's width so a row badge doesn't slide under it. */
export const PIN_TRAILING_PADDING = "pr-12";

/**
 * A menu row with its pin control beside it.
 *
 * The control is a sibling of the menu item, not a child: a control nested
 * inside `role="menuitem"` joins the item's accessible name ("Shells Unpin
 * Shells"), which breaks every by-name lookup of the row. Beside it, the row
 * keeps its own name and the control keeps its own, and the row's hover
 * background still spans the full width because the item is painted underneath.
 *
 * Without a control this returns the row untouched, so the desktop kebab and
 * the fallback kebab render what they render today.
 */
export function withPinControl(pin: ReactNode, row: ReactNode): ReactNode {
  if (pin === null || pin === undefined) return row;
  return (
    // The row stretches to PIN_ROW_CLASS's height, so the 44 px control stays
    // inside the row it belongs to and the label stays centred with it.
    <div data-pin-row className={cn("relative flex w-full items-stretch", PIN_ROW_CLASS)}>
      <div className="flex w-full items-center [&>*]:w-full">{row}</div>
      <span className="pointer-events-none absolute inset-y-0 right-0 flex items-center pr-0.5">
        <span className="pointer-events-auto flex items-center">{pin}</span>
      </span>
    </div>
  );
}
