// One tip component for the Usage board. A tip that carries data is a
// structured card — title, status chip, label/value rows, bars — never a
// sentence with numbers in it; a plain short explanation stays plain text.
// Desktop opens it on hover or focus; on touch the tile goes straight to the
// detail page and its cards open by tap there.

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

import { TONE_TEXT, type UsageTone } from "./usageBoardFormat";

const HOVER_CLOSE_DELAY_MS = 150;

export interface UsageTipRow {
  label: string;
  value: string;
  /** 0–100; renders a bar after the label. */
  bar?: number;
  tone?: UsageTone;
}

export interface UsageTipModel {
  title: string;
  chip?: { label: string; tone: UsageTone };
  /** A short line above the title, e.g. the group a window belongs to. */
  note?: string;
  rows: UsageTipRow[];
  /** A rule before this row index, splitting bars from figures. */
  dividerBefore?: number;
}

const chipTone: Record<UsageTone, string> = {
  ok: "text-success",
  warn: "text-warning",
  crit: "text-destructive",
  mute: "text-muted-foreground",
};

/** The card body. Fixed width so nothing wraps out of the frame. */
export function UsageTipCard({ model }: { model: UsageTipModel }) {
  return (
    <div className="w-[216px] text-xs leading-snug" data-testid="usage-tip-card">
      {model.note ? (
        <div className="mb-0.5 text-[11px] text-muted-foreground">{model.note}</div>
      ) : null}
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-semibold text-foreground">{model.title}</span>
        {model.chip ? (
          <span
            className={cn(
              "inline-flex shrink-0 items-center rounded-full bg-current/12 px-1.5 py-px text-[10px] font-medium",
              chipTone[model.chip.tone],
            )}
          >
            {model.chip.label}
          </span>
        ) : null}
      </div>
      {model.rows.map((row, index) => (
        <div key={`${row.label}-${row.value}`}>
          {model.dividerBefore === index ? <div className="my-1 h-px bg-border" /> : null}
          {row.bar === undefined ? (
            <div className="flex items-center justify-between gap-3 text-muted-foreground">
              <span className="min-w-0 truncate">{row.label}</span>
              <span className="shrink-0 font-semibold text-foreground">{row.value}</span>
            </div>
          ) : (
            <div className="grid grid-cols-[42px_minmax(0,1fr)_44px] items-center gap-2">
              <span className="truncate text-muted-foreground">{row.label}</span>
              <span className="h-1 overflow-hidden rounded-full bg-foreground/10">
                <span
                  className="block h-full rounded-full"
                  style={{
                    width: `${Math.max(0, Math.min(100, row.bar))}%`,
                    background: `var(--${row.tone === "warn" ? "warning" : row.tone === "crit" ? "destructive" : row.tone === "ok" ? "success" : "muted-foreground"})`,
                  }}
                />
              </span>
              <span
                className={cn(
                  "text-right font-semibold",
                  row.tone ? TONE_TEXT[row.tone] : "text-foreground",
                )}
              >
                {row.value}
              </span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

interface UsageTipProps {
  model: UsageTipModel;
  /** The trigger's content; rendered inside a span so it can sit in a tile. */
  children: ReactNode;
  className?: string;
  /**
   * Open on a tap, not only on hover. A board tile leaves this off so the tap
   * goes through to the account's detail page; a detail-page ring turns it on,
   * because there is nowhere else for that tap to go.
   */
  tapOpens?: boolean;
}

/**
 * Hover/focus wrapper for a data card. The trigger never swallows a click, so
 * on a tile the tap that opens the tip on desktop still navigates on a phone.
 */
export function UsageTip({ model, children, className, tapOpens = false }: UsageTipProps) {
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    },
    [],
  );

  function cancelClose() {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }

  function scheduleClose() {
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setOpen(false);
    }, HOVER_CLOSE_DELAY_MS);
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        // A trigger click asks to open; only the surfaces that own that tap
        // accept it, so a tile's click still navigates.
        if (next && !tapOpens) return;
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <span
          className={className}
          tabIndex={tapOpens ? undefined : 0}
          onPointerEnter={(event) => {
            if (event.pointerType !== "mouse") return;
            cancelClose();
            setOpen(true);
          }}
          onPointerLeave={(event) => {
            if (event.pointerType !== "mouse") return;
            scheduleClose();
          }}
          onFocus={(event) => {
            cancelClose();
            if ((event.target as HTMLElement).matches(":focus-visible")) setOpen(true);
          }}
          onBlur={scheduleClose}
        >
          {children}
        </span>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="center"
        className="w-auto p-2.5"
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
        <UsageTipCard model={model} />
      </PopoverContent>
    </Popover>
  );
}

/**
 * A tip button that opens on tap and stays open, for the one card that must be
 * reachable on a phone (the reading's state, as the error triangle is).
 */
export function UsageTipButton({
  model,
  label,
  children,
  className,
}: UsageTipProps & { label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className={cn(
            "inline-flex min-h-11 items-center gap-1 rounded px-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground md:min-h-6",
            className,
          )}
          onClick={(event) => event.stopPropagation()}
        >
          {children}
        </button>
      </PopoverTrigger>
      <PopoverContent side="bottom" align="end" className="w-auto p-2.5">
        <UsageTipCard model={model} />
      </PopoverContent>
    </Popover>
  );
}
