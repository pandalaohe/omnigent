import { CircleHelpIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useHoverPinFloat } from "@/hooks/useHoverPinFloat";
import { cn } from "@/lib/utils";

/**
 * A "?" hint with a popover body. Popover rather than Tooltip because touch
 * has no hover: tap toggles it, a mouse pointer also opens it on hover, and
 * Escape or an outside click closes it (Radix defaults). The float behaviour
 * itself lives in `useHoverPinFloat`, shared with the composer's context ring.
 */
export function HelpTip({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const { open, setOpen, triggerHandlers, contentHandlers } = useHoverPinFloat();

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
          {...triggerHandlers}
        >
          <CircleHelpIcon className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        className="w-64 text-xs"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        {...contentHandlers}
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}
