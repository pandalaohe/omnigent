import { ChevronLeftIcon } from "lucide-react";
import type { ComponentProps, ReactElement, ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
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
  /** Controlled open state, for an editor the page opens without a click. */
  open?: boolean;
  /** One short line under the title. */
  note?: ReactNode;
  /** A longer explanation, kept behind a `?` beside the title. */
  help?: ReactNode;
  /** Steps back out of a level the editor switched into. */
  back?: { label: string; onBack: () => void };
  from?: string;
  savedHere?: boolean;
  useInheritedLabel?: string;
  onUseInherited?: () => void;
  onClear?: () => void;
  clearBelowCount?: number;
  onClearBelow?: () => void;
  /** Quiet page-specific actions, beside the clear actions. */
  actions?: ReactNode;
  /** The editor's own Cancel and Save, always in view at the bottom right. */
  footer?: ReactNode;
  contentClassName?: string;
  onOpenChange?: (open: boolean) => void;
}

/** A quiet action in the editor's bottom bar. */
export function LayeredEditorAction({ className, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={cn(
        "min-h-11 px-2 text-sm font-normal text-muted-foreground md:min-h-7",
        className,
      )}
      {...props}
    />
  );
}

/** Field-neutral popover frame shared by every layered settings editor. */
export function LayeredEditor({
  trigger,
  title,
  children,
  open,
  note,
  help,
  back,
  from,
  savedHere,
  useInheritedLabel,
  onUseInherited,
  onClear,
  clearBelowCount = 0,
  onClearBelow,
  actions,
  footer,
  contentClassName,
  onOpenChange,
}: LayeredEditorProps) {
  const hasQuietActions = useInheritedLabel || onClear || clearBelowCount > 0 || actions;
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          (event.currentTarget as HTMLElement | null)?.focus();
        }}
        align="start"
        collisionPadding={8}
        data-pop=""
        className={cn(
          "max-h-[min(36rem,var(--radix-popover-content-available-height))] menu-surface w-[min(22.5rem,calc(100vw-1rem))] gap-0 overflow-hidden p-0 ring-0",
          contentClassName,
        )}
      >
        <PopoverHeader className="shrink-0 border-b border-border px-3 py-2.5">
          {back && (
            <button
              type="button"
              className="-ml-1 flex min-h-11 w-fit items-center gap-0.5 rounded-sm pr-1 text-sm text-muted-foreground hover:text-foreground md:min-h-6"
              onClick={back.onBack}
            >
              <ChevronLeftIcon className="size-4" />
              {back.label}
            </button>
          )}
          <div className="flex items-center gap-1.5">
            <PopoverTitle className="min-w-0 font-semibold wrap-anywhere">{title}</PopoverTitle>
            {help != null && (
              <HelpTip label={`About ${title}`} className="-my-3 size-11 md:my-0 md:size-4">
                {help}
              </HelpTip>
            )}
          </div>
          {note != null && <PopoverDescription className="text-xs">{note}</PopoverDescription>}
          {from && <PopoverDescription className="text-xs">From {from}</PopoverDescription>}
          {savedHere && <PopoverDescription className="text-xs">Saved here</PopoverDescription>}
        </PopoverHeader>
        <div className="flex min-h-0 min-w-0 flex-col gap-3 overflow-y-auto p-3 [scrollbar-width:thin]">
          {children}
        </div>
        {(hasQuietActions || footer) && (
          <div className="flex shrink-0 flex-wrap items-center gap-x-1 gap-y-1.5 border-t border-border py-2 pr-3 pl-1">
            {useInheritedLabel && onUseInherited && (
              <LayeredEditorAction onClick={onUseInherited}>
                {useInheritedLabel}
              </LayeredEditorAction>
            )}
            {onClear && (
              <LayeredEditorAction onClick={onClear}>Clear this level</LayeredEditorAction>
            )}
            {clearBelowCount > 0 && onClearBelow && (
              <LayeredEditorAction onClick={onClearBelow}>
                Clear {clearBelowCount} {clearBelowCount === 1 ? "value" : "values"}
              </LayeredEditorAction>
            )}
            {actions}
            {footer && <div className="ml-auto flex items-center gap-2">{footer}</div>}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
