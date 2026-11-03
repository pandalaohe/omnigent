import type { ReactElement, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export interface LayeredEditorProps {
  trigger: ReactElement;
  title: string;
  children: ReactNode;
  from?: string;
  savedHere?: boolean;
  useInheritedLabel?: string;
  onUseInherited?: () => void;
  onClear?: () => void;
  clearBelowCount?: number;
  onClearBelow?: () => void;
  actions?: ReactNode;
  contentClassName?: string;
  /** Controlled open state, for an editor the page opens without a click. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

/** Field-neutral popover frame shared by every layered settings editor. */
export function LayeredEditor({
  trigger,
  title,
  children,
  from,
  savedHere,
  useInheritedLabel,
  onUseInherited,
  onClear,
  clearBelowCount = 0,
  onClearBelow,
  actions,
  contentClassName,
  open,
  onOpenChange,
}: LayeredEditorProps) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        align="start"
        collisionPadding={8}
        data-pop=""
        className={cn(
          "max-h-[min(32rem,var(--radix-popover-content-available-height))] w-80 overflow-y-auto",
          contentClassName,
        )}
      >
        <PopoverHeader>
          <PopoverTitle>{title}</PopoverTitle>
          {from && <PopoverDescription>From {from}</PopoverDescription>}
          {savedHere && <PopoverDescription>Saved here</PopoverDescription>}
        </PopoverHeader>
        <div className="flex min-w-0 flex-col gap-2.5">{children}</div>
        {(useInheritedLabel || onClear || clearBelowCount > 0 || actions) && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2.5">
            {useInheritedLabel && onUseInherited && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="min-h-11 md:min-h-7"
                onClick={onUseInherited}
              >
                {useInheritedLabel}
              </Button>
            )}
            {onClear && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="min-h-11 md:min-h-7"
                onClick={onClear}
              >
                Clear this level
              </Button>
            )}
            {clearBelowCount > 0 && onClearBelow && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="min-h-11 md:min-h-7"
                onClick={onClearBelow}
              >
                Clear {clearBelowCount} {clearBelowCount === 1 ? "value" : "values"}
              </Button>
            )}
            {actions}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
