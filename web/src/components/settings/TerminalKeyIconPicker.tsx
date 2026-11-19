import { useRef, useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";

import { TERMINAL_KEY_ICON_PAGES } from "@/components/blocks/TerminalKeyContent";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const PAGE_NAMES = ["Keys", "Tools and agents", "Symbols"];

export function TerminalKeyIconPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (icon: string) => void;
}) {
  const [page, setPage] = useState(() =>
    Math.max(
      0,
      TERMINAL_KEY_ICON_PAGES.findIndex((icons) => Object.hasOwn(icons, value)),
    ),
  );
  const start = useRef<{ id: number; x: number; y: number } | null>(null);
  const swiped = useRef(false);
  const lastPage = TERMINAL_KEY_ICON_PAGES.length - 1;
  const move = (step: number) =>
    setPage((current) => Math.max(0, Math.min(lastPage, current + step)));

  return (
    <div className="grid gap-1" role="group" aria-label="Terminal key icons">
      <div
        className="grid touch-pan-y grid-cols-6 justify-items-center gap-y-1"
        onPointerDown={(event) => {
          if (!event.isPrimary) return;
          swiped.current = false;
          if (event.pointerType === "touch") {
            start.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
          }
        }}
        onPointerCancel={() => {
          start.current = null;
        }}
        onPointerUp={(event) => {
          const origin = start.current;
          if (origin?.id !== event.pointerId) return;
          start.current = null;
          const dx = event.clientX - origin.x;
          const dy = event.clientY - origin.y;
          if (Math.abs(dx) < 30 || Math.abs(dx) <= Math.abs(dy)) return;
          swiped.current = true;
          move(dx < 0 ? 1 : -1);
        }}
        onClickCapture={(event) => {
          if (swiped.current && event.detail !== 0) {
            event.preventDefault();
            event.stopPropagation();
          }
        }}
      >
        {Object.entries(TERMINAL_KEY_ICON_PAGES[page]).map(([id, Icon]) => {
          const selected = id === value || (id === "delete" && value === "backspace");
          const name = id.replace(/^agent-/, "").replaceAll("-", " ");
          return (
            <Button
              key={id}
              type="button"
              size="icon"
              variant="outline"
              className={cn("size-11", selected && "border-primary bg-primary/10 text-primary")}
              aria-label={name}
              aria-pressed={selected}
              title={name}
              onClick={() => onChange(id)}
            >
              <Icon className="size-4" aria-hidden />
            </Button>
          );
        })}
      </div>
      <div className="flex items-center justify-between gap-2">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-11"
          aria-label="Previous icon page"
          disabled={page === 0}
          onClick={() => move(-1)}
        >
          <ChevronLeftIcon aria-hidden />
        </Button>
        <span className="text-center text-sm text-muted-foreground" role="status">
          {PAGE_NAMES[page]} · Page {page + 1} of {TERMINAL_KEY_ICON_PAGES.length}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-11"
          aria-label="Next icon page"
          disabled={page === lastPage}
          onClick={() => move(1)}
        >
          <ChevronRightIcon aria-hidden />
        </Button>
      </div>
    </div>
  );
}
