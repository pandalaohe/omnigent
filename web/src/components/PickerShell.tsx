import { useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

export function PickerShell({
  trigger,
  title,
  description,
  open: controlledOpen,
  onOpenChange,
  footer,
  children,
}: {
  trigger: ReactNode;
  title: string;
  description?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  const [ownOpen, setOwnOpen] = useState(false);
  const open = controlledOpen ?? ownOpen;
  const setOpen = onOpenChange ?? setOwnOpen;
  const mobile = useIsMobileViewport();
  const content = (
    <>
      {mobile ? (
        <DialogHeader className="border-b px-5 py-4">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className={description ? undefined : "sr-only"}>
            {description ?? title}
          </DialogDescription>
        </DialogHeader>
      ) : (
        <div className="border-b px-4 py-3 text-sm font-medium">{title}</div>
      )}
      <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
      {footer && <div className="flex justify-end gap-2 border-t px-4 py-3">{footer}</div>}
    </>
  );
  return mobile ? (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="top-auto bottom-0 left-0 flex max-h-[min(640px,90dvh)] w-full max-w-full translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-b-none p-0">
        {content}
      </DialogContent>
    </Dialog>
  ) : (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={6}
        role="dialog"
        aria-label={title}
        className="flex max-h-[min(640px,80dvh)] w-[min(560px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0"
      >
        {content}
      </PopoverContent>
    </Popover>
  );
}
